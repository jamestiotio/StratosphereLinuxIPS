import collections
import contextlib
import json
import math
from typing import List
import dns.resolver
import dns.query
import dns.message
import validators

from modules.flowalerts.timer_thread import TimerThread
from slips_files.common.abstracts.flowalerts_analyzer import (
    IFlowalertsAnalyzer,
)
from slips_files.common.flow_classifier import FlowClassifier
from slips_files.common.parsers.config_parser import ConfigParser
from slips_files.common.slips_utils import utils
from slips_files.core.structures.evidence import Direction


class DNS(IFlowalertsAnalyzer):
    def init(self):
        self.read_configuration()
        # this dict will contain the number of nxdomains
        # found in every profile
        self.nxdomains = {}
        # if nxdomains are >= this threshold, it's probably DGA
        self.nxdomains_threshold = 10
        # Cache list of connections that we already checked in the timer
        # thread (we waited for the connection of these dns resolutions)
        self.connections_checked_in_dns_conn_timer_thread = []
        # dict to keep track of arpa queries to check for DNS arpa scans later
        # format {profileid: [ts,ts,...]}
        self.dns_arpa_queries = {}
        # after this number of arpa queries, slips will detect an arpa scan
        self.arpa_scan_threshold = 10
        self.classifier = FlowClassifier()

    def name(self) -> str:
        return "DNS_analyzer"

    def read_configuration(self):
        conf = ConfigParser()
        self.shannon_entropy_threshold = conf.get_entropy_threshold()

    def is_dns_server(self, ip: str) -> bool:
        """checks if the given IP is a DNS server by making a query and
        waiting for a response"""
        try:
            query = dns.message.make_query("google.com", dns.rdatatype.A)
            dns.query.udp(query, ip, timeout=2)
            return True
        except Exception:
            # If there's any error, the IP is probably not a DNS server
            return False

    @staticmethod
    def should_detect_dns_without_conn(flow) -> bool:
        """
        returns False in the following cases
         - All reverse dns resolutions
         - All .local domains
         - The wildcard domain *
         - Subdomains of cymru.com, since it is used by
         the ipwhois library in Slips to get the ASN
         of an IP and its range. This DNS is meant not
         to have a connection later
         - Domains check from Chrome, like xrvwsrklpqrw
         - The WPAD domain of windows
         - When there is an NXDOMAIN as answer, it means
         the domain isn't resolved, so we should not expect any
            connection later
        """
        if (
            "arpa" in flow.query
            or ".local" in flow.query
            or "*" in flow.query
            or ".cymru.com" in flow.query[-10:]
            or len(flow.query.split(".")) == 1
            or flow.query == "WPAD"
            or flow.rcode_name != "NOERROR"
            or not flow.answers
        ):
            return False
        return True

    def is_cname_contacted(self, answers, contacted_ips) -> bool:
        """
        check if any ip of the given CNAMEs is contacted
        """
        for CNAME in answers:
            if not utils.is_valid_domain(CNAME):
                # it's an ip
                continue
            ips = self.db.get_domain_resolution(CNAME)
            for ip in ips:
                if ip in contacted_ips:
                    return True
        return False

    @staticmethod
    def should_detect_young_domain(domain):
        """
        returns true if it's ok to detect young domains for the given
        domain
        """
        return (
            domain
            and not domain.endswith(".local")
            and not domain.endswith(".arpa")
        )

    def detect_young_domains(self, profileid, twid, flow):
        """
        Detect domains that are too young.
        The threshold is 60 days
        """
        if not self.should_detect_young_domain(flow.query):
            return False

        age_threshold = 60

        domain_info: dict = self.db.get_domain_data(flow.query)
        if not domain_info:
            return False

        if "Age" not in domain_info:
            # we don't have age info about this domain
            return False

        # age is in days
        age = domain_info["Age"]
        if age >= age_threshold:
            return False

        ips_returned_in_answer: List[str] = self.extract_ips_from_dns_answers(
            flow.answers
        )
        self.set_evidence.young_domain(twid, flow, age, ips_returned_in_answer)
        return True

    @staticmethod
    def extract_ips_from_dns_answers(answers: List[str]) -> List[str]:
        """
        extracts ipv4 and 6 from DNS answers
        """
        ips = []
        for answer in answers:
            if validators.ipv4(answer) or validators.ipv6(answer):
                ips.append(answer)
        return ips

    def is_connection_made_by_different_version(self, profileid, twid, daddr):
        """
        :param daddr: the ip this connection is made to (destination ip)
        """
        # get the other ip version of this computer
        other_ip = self.db.get_the_other_ip_version(profileid)
        if not other_ip:
            return False
        other_ip = other_ip[0]
        # get the ips contacted by the other_ip
        contacted_ips = self.db.get_all_contacted_ips_in_profileid_twid(
            f"profile_{other_ip}", twid
        )
        if not contacted_ips:
            return False

        if daddr in contacted_ips:
            # now we're sure that the connection was made
            # by this computer but using a different ip version
            return True

    def check_dns_without_connection(self, profileid, twid, flow):
        """
        Makes sure all cached DNS answers are used in contacted_ips
        """
        if not self.should_detect_dns_without_conn(flow):
            return False

        # One DNS query may not be answered exactly by UID,
        # but the computer can re-ask the domain,
        # and the next DNS resolution can be
        # answered. So dont check the UID, check if the domain has an IP

        # self.print(f'The DNS query to {domain} had as answers {answers} ')

        # It can happen that this domain was already resolved
        # previously, but with other IPs
        # So we get from the DB all the IPs for this domain
        # first and append them to the answers
        # This happens, for example, when there is 1 DNS
        # resolution with A, then 1 DNS resolution
        # with AAAA, and the computer chooses the A address.
        # Therefore, the 2nd DNS resolution
        # would be treated as 'without connection', but this is false.
        if prev_domain_resolutions := self.db.get_domain_data(flow.query):
            prev_domain_resolutions = prev_domain_resolutions.get("IPs", [])
            # if there's a domain in the cache
            # (prev_domain_resolutions) that is not in the
            # current answers given to this function,
            # append it to the answers list
            flow.answers.extend(
                [
                    ans
                    for ans in prev_domain_resolutions
                    if ans not in flow.answers
                ]
            )

        if flow.answers == ["-"]:
            # If no IPs are in the answer, we can not expect
            # the computer to connect to anything
            # self.print(f'No ips in the answer, so ignoring')
            return False

        contacted_ips = self.db.get_all_contacted_ips_in_profileid_twid(
            profileid, twid
        )
        # If contacted_ips is empty it can be because
        # we didnt read yet all the flows.
        # This is automatically captured later in the
        # for loop and we start a Timer

        # every dns answer is a list of ips that correspond to 1 query,
        # one of these ips should be present in the contacted ips
        # check each one of the resolutions of this domain
        for ip in self.extract_ips_from_dns_answers(flow.answers):
            # self.print(f'Checking if we have a connection to ip {ip}')
            if (
                ip in contacted_ips
                or self.is_connection_made_by_different_version(
                    profileid, twid, ip
                )
            ):
                # this dns resolution has a connection. We can exit
                return False

        # Check if there was a connection to any of the CNAMEs
        if self.is_cname_contacted(flow.answers, contacted_ips):
            # this is not a DNS without resolution
            return False

        # self.print(f'It seems that none of the IPs were contacted')
        # Found a DNS query which none of its IPs was contacted
        # It can be that Slips is still reading it from the files.
        # Lets check back in some time
        # Create a timer thread that will wait some seconds for the
        # connection to arrive and then check again
        if flow.uid not in self.connections_checked_in_dns_conn_timer_thread:
            # comes here if we haven't started the timer
            # thread for this dns before mark this dns as checked
            self.connections_checked_in_dns_conn_timer_thread.append(flow.uid)
            params = [profileid, twid, flow]
            # self.print(f'Starting the timer to check on {domain}, uid {uid}.
            # time {datetime.datetime.now()}')
            timer = TimerThread(40, self.check_dns_without_connection, params)
            timer.start()
        else:
            # It means we already checked this dns with the Timer process
            # but still no connection for it.
            self.set_evidence.dns_without_conn(profileid, twid, flow)
            # This UID will never appear again, so we can remove it and
            # free some memory
            with contextlib.suppress(ValueError):
                self.connections_checked_in_dns_conn_timer_thread.remove(
                    flow.uid
                )

    @staticmethod
    def estimate_shannon_entropy(string):
        m = len(string)
        bases = collections.Counter(list(string))
        shannon_entropy_value = 0
        for base in bases:
            # number of residues
            n_i = bases[base]
            # n_i (# residues type i) / M (# residues in column)
            p_i = n_i / float(m)
            entropy_i = p_i * (math.log(p_i, 2))
            shannon_entropy_value += entropy_i

        return shannon_entropy_value * -1

    def check_high_entropy_dns_answers(self, profileid, twid, flow):
        """
        Uses shannon entropy to detect DNS TXT answers
        with encoded/encrypted strings
        """
        if not flow.answers:
            return

        for answer in flow.answers:
            if "TXT" not in answer:
                continue
            entropy = self.estimate_shannon_entropy(answer)
            if entropy >= self.shannon_entropy_threshold:
                self.set_evidence.suspicious_dns_answer(profileid, twid, flow)

    def check_invalid_dns_answers(self, profileid, twid, flow):
        # this function is used to check for certain IP
        # answers to DNS queries being blocked
        # (perhaps by ad blockers) and set to the following IP values
        # currently hardcoding blocked ips
        invalid_answers = {"127.0.0.1", "0.0.0.0"}
        if not flow.answers:
            return

        for answer in flow.answers:
            if answer in invalid_answers and flow.query != "localhost":
                # blocked answer found
                self.set_evidence.invalid_dns_answer(profileid, twid, flow)
                # delete answer from redis cache to prevent
                # associating this dns answer with this domain/query and
                # avoid FP "DNS without connection" evidence
                self.db.delete_dns_resolution(flow.answer)

    def detect_dga(self, profileid, twid, flow):
        """
        Detect DGA based on the amount of NXDOMAINs seen in dns.log
        alerts when 10 15 20 etc. nxdomains are found
        Ignore queries done to *.in-addr.arpa domains and to *.local domains
        """
        if not flow.rcode_name:
            return

        # check whitelisted queries because we
        # don't want to count nxdomains to cymru.com or
        # spamhaus as DGA as they're made
        # by slips
        if (
            "NXDOMAIN" not in flow.rcode_name
            or not flow.query
            or flow.query.endswith(".arpa")
            or flow.query.endswith(".local")
            or self.flowalerts.whitelist.domain_analyzer.is_whitelisted(
                flow.query, Direction.SRC, "alerts"
            )
        ):
            return False

        profileid_twid = f"{profileid}_{twid}"

        # found NXDOMAIN by this profile
        try:
            # make sure all domains are unique
            if flow.query not in self.nxdomains[profileid_twid]:
                queries, uids = self.nxdomains[profileid_twid]
                queries.append(flow.query)
                uids.append(flow.uid)
                self.nxdomains[profileid_twid] = (queries, uids)
        except KeyError:
            # first time seeing nxdomain in this profile and tw
            self.nxdomains.update({profileid_twid: ([flow.query], [flow.uid])})
            return False

        # every 5 nxdomains, generate an alert.
        queries, uids = self.nxdomains[profileid_twid]
        number_of_nxdomains = len(queries)
        if (
            number_of_nxdomains % 5 == 0
            and number_of_nxdomains >= self.nxdomains_threshold
        ):
            self.set_evidence.dga(
                profileid, twid, flow, number_of_nxdomains, uids
            )
            # clear the list of alerted queries and uids
            self.nxdomains[profileid_twid] = ([], [])
            return True

    def check_dns_arpa_scan(self, profileid, twid, flow):
        """
        Detect and ARPA scan if an ip performed 10(arpa_scan_threshold)
        or more arpa queries within 2 seconds
        """
        if not flow.query:
            return False
        if not flow.query.endswith(".in-addr.arpa"):
            return False

        try:
            # format of this dict is
            # {profileid: [stime of first arpa query, stime of second, etc..]}
            timestamps, uids, domains_scanned = self.dns_arpa_queries[
                profileid
            ]
            timestamps.append(flow.starttime)
            uids.append(flow.uid)
            domains_scanned.add(flow.query)
            self.dns_arpa_queries[profileid] = (
                timestamps,
                uids,
                domains_scanned,
            )
        except KeyError:
            # first time for this profileid to perform an arpa query
            self.dns_arpa_queries[profileid] = (
                [flow.starttime],
                [flow.uid],
                {flow.query},
            )
            return False

        if len(domains_scanned) < self.arpa_scan_threshold:
            # didn't reach the threshold yet
            return False

        # reached the threshold, did the 10 queries happen within 2 seconds?
        diff = utils.get_time_diff(timestamps[0], timestamps[-1])
        if diff > 2:
            # happened within more than 2 seconds
            return False

        self.set_evidence.dns_arpa_scan(
            profileid, twid, flow, self.arpa_scan_threshold, uids
        )
        # empty the list of arpa queries for this profile,
        # we don't need them anymore
        self.dns_arpa_queries.pop(profileid)
        return True

    def analyze(self, msg):
        if not utils.is_msg_intended_for(msg, "new_dns"):
            return False
        msg = json.loads(msg["data"])
        profileid = msg["profileid"]
        twid = msg["twid"]
        flow = self.classifier.convert_to_flow_obj(msg["flow"])
        self.check_dns_without_connection(profileid, twid, flow)
        self.check_high_entropy_dns_answers(profileid, twid, flow)
        self.check_invalid_dns_answers(profileid, twid, flow)
        self.detect_dga(profileid, twid, flow)
        # TODO: not sure how to make sure IP_info is
        #  done adding domain age to the db or not
        self.detect_young_domains(profileid, twid, flow)
        self.check_dns_arpa_scan(profileid, twid, flow)
