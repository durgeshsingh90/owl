"""Global search over everything downloaded: each account's state.json and nslookup results.

Results are ranked by how well they match: exact name or id, then all words, then most words,
then near matches (typos). An IP address also finds resources whose CIDR contains it. The index
keeps text only and is rebuilt when a download changes the files; full properties are read from
state.json when a result is opened.
"""

import difflib
import ipaddress
import json
import re
import threading
from collections import Counter
from pathlib import Path

from app.architecture import download

_lock = threading.Lock()
_index = {"signature": None, "documents": [], "words": {}}
WORD = re.compile(r"[a-z][a-z-]{3,}")
NEAR_CUTOFF = 0.78
MAX_SNIPPETS = 3
TITLES = {
    "aws_vpc": "VPC", "aws_subnet": "Subnet", "aws_route_table": "Route table", "aws_security_group": "Security group",
    "aws_network_acl": "Network ACL", "aws_nat_gateway": "NAT gateway", "aws_internet_gateway": "Internet gateway",
    "aws_vpc_endpoint": "VPC endpoint", "aws_vpc_endpoint_service": "Endpoint service", "aws_vpc_endpoint_connection": "Endpoint connection",
    "aws_ec2_transit_gateway": "Transit gateway", "aws_ec2_transit_gateway_vpc_attachment": "TGW VPC attachment",
    "aws_ec2_transit_gateway_attachment": "TGW attachment", "aws_ec2_transit_gateway_route_table": "TGW route table",
    "aws_ec2_transit_gateway_peering_attachment": "TGW peering", "aws_ec2_transit_gateway_connect": "TGW Connect",
    "aws_lb": "Load balancer", "aws_lb_listener": "Listener", "aws_lb_target_group": "Target group", "aws_elb": "Classic load balancer",
    "aws_eks_cluster": "EKS cluster", "aws_eks_node_group": "EKS node group", "aws_eks_fargate_profile": "EKS Fargate profile", "aws_eks_addon": "EKS add-on",
    "aws_instance": "EC2 instance", "aws_autoscaling_group": "Auto Scaling group", "aws_launch_template": "Launch template",
    "aws_networkfirewall_firewall": "Network Firewall", "aws_route53_zone": "Route 53 zone", "aws_route53_record": "Route 53 record",
    "aws_route53_resolver_rule": "Resolver rule", "aws_route53_resolver_rule_association": "Resolver association",
    "aws_route53_resolver_endpoint": "Resolver endpoint", "aws_vpc_peering_connection": "VPC peering", "aws_vpn_connection": "VPN connection",
    "aws_vpn_gateway": "VPN gateway", "aws_customer_gateway": "Customer gateway", "aws_dx_connection": "Direct Connect connection",
    "aws_dx_virtual_interface": "Direct Connect virtual interface", "aws_dx_gateway": "Direct Connect gateway",
    "aws_ec2_managed_prefix_list": "Prefix list", "aws_egress_only_internet_gateway": "Egress-only gateway", "aws_eip": "Elastic IP",
    "aws_vpc_dhcp_options": "DHCP options", "aws_network_interface": "Network interface", "aws_api_gateway_rest_api": "REST API",
    "aws_api_gateway_vpc_link": "REST VPC link", "aws_apigatewayv2_api": "HTTP API", "aws_apigatewayv2_vpc_link": "HTTP VPC link",
    "dns_lookup": "DNS lookup", "aws_account": "AWS account",
}
SKIP = {"certificateAuthority"}  # long and never useful to search


def title(kind):
    return TITLES.get(kind, kind.replace("aws_", "").replace("_", " ").title())


def flatten(value, path=""):
    """(path, text) for every value. Tags read as Tags.<key> = value."""
    if isinstance(value, dict):
        if set(value) >= {"Key", "Value"} and len(value) <= 3:
            yield (f"Tags.{value['Key']}", str(value["Value"]))
            yield (f"Tags.{value['Key']}.key", str(value["Key"]))
            return
        for key, item in value.items():
            if key not in SKIP:
                yield from flatten(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for item in value:
            yield from flatten(item, path)
    elif value is not None and value != "" and not isinstance(value, bool):
        yield (path, str(value)[:500])


class Document:
    __slots__ = ("kind", "id", "name", "account", "account_name", "env", "region", "created", "file", "index", "pairs", "text", "head", "cidrs", "tags")

    def __init__(self, kind, resource_id, name, account, account_name, env, region, created, file, index, attributes, tags=()):
        self.tags = list(tags)
        self.kind, self.id, self.name = kind, str(resource_id), str(name or "")
        self.account, self.account_name, self.env, self.region, self.created = account, account_name, env, region, created
        self.file, self.index = str(file), index
        self.pairs = "\n".join(f"{path}\t{text}" for path, text in flatten(attributes))
        values = [line.split("\t", 1)[1] for line in self.pairs.splitlines() if "\t" in line]
        self.head = f"{self.name}\n{self.id}".lower()
        self.text = "\n" + "\n".join([self.name, self.id, title(kind), account, account_name, env, region, *self.tags, *values]).lower() + "\n"
        self.cidrs = tuple(sorted({v for v in values if "/" in v and re.fullmatch(r"[0-9a-fA-F:.]+/\d+", v)}))

    def summary(self):
        return {"type": self.kind, "type_title": title(self.kind), "id": self.id, "name": self.name, "account": self.account,
                "account_name": self.account_name, "env": self.env, "region": self.region, "created_at": self.created,
                "file": self.file, "index": self.index, "tags": self.tags}


def _state_files():
    """The newest download folder of each account."""
    latest = {}
    for path in sorted(download.media_folder().glob("*-*/*/account.json"), key=lambda p: p.stat().st_mtime):
        latest[path.parent.parent.name.split("-", 1)[0]] = path.parent
    return latest


def _signature(folders):
    tags = download.tags_file()
    stamps = [("tags", "", tags.stat().st_mtime_ns if tags.exists() else 0)]
    for account, folder in sorted(folders.items()):
        for name in ("state.json", "account.json", "global/dns-lookups.json"):
            path = folder / name
            stamps.append((account, name, path.stat().st_mtime if path.exists() else 0))
    return tuple(stamps)


def _build(folders):
    documents = []
    tags = download.account_tags()
    for account, folder in sorted(folders.items()):
        labels = tags.get(account, [])
        account_name = folder.parent.name.split("-", 1)[1] if "-" in folder.parent.name else ""
        env = folder.name
        manifest_path = folder / "account.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        info = manifest.get("Account", {})
        documents.append(Document("aws_account", account, f"{account_name} {env}".strip(), account, account_name, env, info.get("Region", ""),
                                  info.get("Created", ""), manifest_path, -1, info, labels))
        state_path = folder / "state.json"
        if state_path.exists():
            for index, item in enumerate(json.loads(state_path.read_text()).get("resources", [])):
                documents.append(Document(item.get("type", ""), item.get("id", ""), item.get("name", ""), account, account_name, env,
                                          item.get("region", ""), item.get("created_at", ""), state_path, index, item.get("attributes", {}), labels))
        lookups = folder / "global" / "dns-lookups.json"
        if lookups.exists():
            for index, item in enumerate(json.loads(lookups.read_text()).get("DnsLookups", [])):
                documents.append(Document("dns_lookup", item.get("name", ""), item.get("name", ""), account, account_name, env, "global",
                                          item.get("looked_up_at", ""), lookups, index, item, labels))
    words = {}
    for word in {w for document in documents for w in WORD.findall(document.text)}:
        words.setdefault(len(word), []).append(word)
    return documents, words


def index():
    folders = _state_files()
    signature = _signature(folders)
    with _lock:
        if _index["signature"] != signature:
            _index["documents"], _index["words"] = _build(folders)
            _index["signature"] = signature
        return _index["documents"], _index["words"]


def _near_words(token, words):
    """Words in the data spelled nearly like this one, for typos."""
    if len(token) < 4 or not re.fullmatch(r"[a-z-]+", token):
        return {}
    candidates = [w for size in range(len(token) - 2, len(token) + 3) for w in words.get(size, [])]
    close = difflib.get_close_matches(token, candidates, n=8, cutoff=NEAR_CUTOFF)
    return {word: difflib.SequenceMatcher(None, token, word).ratio() for word in close if word != token}


def _as_ip(token):
    try:
        return ipaddress.ip_address(token)
    except ValueError:
        return None


def _in_cidrs(address, cidrs):
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if network.version == address.version and address in network and network.prefixlen > 0:
            return cidr
    return ""


def _score_token(document, token):
    """(points, how): direct matches are worth most in the name or id, less anywhere else."""
    head = document.head
    if f"\n{token}\n" in f"\n{head}\n":
        return 100, "exact"
    if head.startswith(token) or f"\n{token}" in head:
        return 70, "direct"
    if token in head:
        return 50, "direct"
    if f"\n{token}\n" in document.text:
        return 40, "exact"
    if token in document.text:
        return 15, "direct"
    return 0, ""


def _snippets(document, tokens):
    found = []
    for line in document.pairs.splitlines():
        path, _, value = line.partition("\t")
        low = value.lower()
        if not path.endswith(".key") and any(t in low for t in tokens):
            found.append({"field": path, "value": value})
            if len(found) == MAX_SNIPPETS:
                break
    return found


def search(query, kind="", account="", limit=100, offset=0):
    documents, words = index()
    tokens = list(dict.fromkeys(query.lower().split()))
    pool = [d for d in documents if (not kind or d.kind == kind) and (not account or d.account == account)]
    types, accounts = Counter(d.kind for d in documents), Counter(d.account for d in documents)
    base = {"query": query, "resources": len(documents), "accounts": len(accounts),
            "types": [{"type": k, "title": title(k), "count": n} for k, n in sorted(types.items(), key=lambda i: title(i[0]))]}
    if not tokens:
        return {**base, "total": 0, "results": [], "tiers": {}, "near_words": {}}
    near = {t: _near_words(t, words) for t in tokens if not any(t in d.text for d in documents)}
    addresses = {t: _as_ip(t) for t in tokens}
    phrase = query.lower().strip()
    scored = []
    for document in pool:
        points = matched = exact = near_hits = 0
        terms, via_cidr = [], ""
        for token in tokens:
            value, how = _score_token(document, token)
            if not value and addresses[token]:
                via_cidr = _in_cidrs(addresses[token], document.cidrs)
                if via_cidr:
                    value, how = 30, "direct"
                    terms.append(via_cidr)
            if not value and near.get(token):
                best = max((ratio for word, ratio in near[token].items() if word in document.text), default=0)
                if best:
                    value, how = int(20 * best), "near"
                    near_hits += 1
                    terms.extend(word for word in near[token] if word in document.text)
            if value:
                points += value
                matched += how != "near"
                exact += how == "exact"
                if how != "near" and not via_cidr:
                    terms.append(token)
        if not points:
            continue
        if len(tokens) > 1 and phrase in document.text:
            points += 60
        if matched == len(tokens):
            tier = "Exact" if exact == len(tokens) else "All words"
        elif matched:
            tier = "Most words" if matched * 2 >= len(tokens) else "Some words"
        else:
            tier = "Near match"
        scored.append((-(matched * 1000 + near_hits * 300 + points), document.name.lower(), document.id, document, tier, terms, via_cidr, matched, near_hits))
    scored.sort(key=lambda item: item[:3])
    results = []
    for score, _, _, document, tier, terms, via_cidr, matched, near_hits in scored[offset:offset + limit]:
        results.append({**document.summary(), "tier": tier, "score": -score, "matched": matched + near_hits, "of": len(tokens),
                        "contains": via_cidr, "snippets": _snippets(document, terms)})
    return {**base, "total": len(scored), "results": results, "tiers": dict(Counter(item[4] for item in scored)),
            "near_words": {t: sorted(w, key=w.get, reverse=True) for t, w in near.items() if w}}


def resource(file, position):
    """A result's full properties, from the download's own files only."""
    documents, _ = index()
    document = next((d for d in documents if d.file == file and d.index == position), None)
    if not document:
        raise KeyError("That result is no longer in the downloads.")
    data = json.loads(Path(document.file).read_text())
    if document.kind == "aws_account":
        attributes = data.get("Account", {})
    elif document.kind == "dns_lookup":
        attributes = data["DnsLookups"][position]
    else:
        attributes = data["resources"][position].get("attributes", {})
    return {**document.summary(), "attributes": attributes}
