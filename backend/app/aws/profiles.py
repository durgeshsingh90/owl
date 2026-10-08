"""AWS accounts from the AWS CLI config file (~/.aws/config), grouped by name.

Every `[profile …]` becomes an account. Its category comes from words in the profile
name (mc-egressnetworkingpalo-prod → Networking), and its environment from the name's
last part (-prod → Prod). The first matching rule wins, so specific groups come first:
"centralized" goes to Shared Services before "networking" can claim it.
"""

import configparser
import os
import re
from collections import Counter
from pathlib import Path

# (category, words) in priority order. A word matches anywhere in the profile name,
# without the environment suffix (so "egressnetworkingpalo" matches "network").
CATEGORY_RULES = [
    ("Shared Services", [
        "centralized", "centralised", "sharedservice", "shared-service", "sharedsvc", "shared",
        "commoncd", "argocd-hub", "landingzone", "landing-zone", "activedirectory", "directory",
        "b2eshared", "rtcshared", "ethocacommon",
    ]),
    ("Management & Billing", [
        "management", "mgmt", "orgroot", "org-root", "organization", "organisation", "payer",
        "billing", "controltower", "control-tower", "governance", "admin", "accessmanagement",
        "technologybusiness", "enterprisereference", "settlementprofile",
    ]),
    ("Security & Privacy", [
        "security", "secops", "infosec", "cyber", "guardduty", "securityhub", "audit", "siem",
        "kms", "hsm", "pki", "certificate", "secrets", "vault", "compliance", "forensic",
        "pentest", "privacy", "cryptographic", "sensitivedata", "confidential", "keymanagement",
        "securityevent", "aisecurity",
    ]),
    ("Identity & Access", [
        "identity", "-iam", "iam-", "idp", "-sso", "sso-", "okta", "pingfed", "cognito",
        "authn", "authentication", "authorization", "secureaccess", "access", "knowya",
        "knowyouragent", "sovrin", "userpermission", "permission", "directservices",
        "tokenauthenticat", "clouddataaccess",
    ]),
    ("Networking", [
        "network", "egress", "ingress", "palo", "firewall", "fw-", "-fw", "vpc", "transit",
        "tgw", "dns", "route53", "akamai", "cdn", "cloudfront", "natgw", "proxy", "waf",
        "vpn", "directconnect", "loadbalanc", "edgeservice", "nextedge", "edge-site",
        "connectivity", "peering", "ipam", "endpointservice", "vmip", "vll-",
    ]),
    ("Logging & Monitoring", [
        "logarchive", "log-archive", "logging", "-logs", "-log-", "monitor", "observab",
        "splunk", "datadog", "dynatrace", "grafana", "prometheus", "cloudwatch", "newrelic",
        "elastic", "opensearch", "telemetry", "apm", "servicecatalogspok",
    ]),
    ("Backup & DR", ["backup", "disaster", "recovery", "resilien", "failover"]),
    ("AI & ML", [
        "genai", "llm", "gpt", "agentic", "aifoundation", "aiecosystem", "aicloud",
        "aidefeature", "aiandml", "aiassistant", "aiinsights", "aitooling", "aiobservab",
        "machinelearning", "mlops", "mlengineering", "mleng", "sagemaker", "bedrock",
        "genaicode", "assistant", "inclusivegrowth", "collaborativeintelligence", "intelligentsolution",
        "intelligentmedia", "knowledgebot", "translate",
    ]),
    ("Data & Analytics", [
        "data", "analytic", "datalake", "lakehouse", "lake", "warehouse", "etl", "glue",
        "redshift", "snowflake", "emr", "databricks", "athena", "reporting", "reports",
        "insight", "intelligence", "dcp", "diagnose", "measurement", "analyzer", "harbr",
        "bmidata", "onedatastrategy", "decisioncaching", "cdp", "udap",
    ]),
    ("Streaming & Messaging", [
        "kafka", "msk", "stream", "kinesis", "messag", "queue", "sqs", "sns", "eventbus",
        "eventbridge", "eventbroker", "eventframework", "rabbitmq", "activemq",
    ]),
    ("Databases", [
        "database", "db-", "-db", "dbengineering", "rds", "aurora", "dynamo", "postgres",
        "mysql", "oracle", "mongo", "redis", "cache",
    ]),
    ("Platform & DevOps", [
        "platform", "devops", "cicd", "ci-cd", "pipeline", "build", "deploy", "jenkins",
        "artifactory", "nexus", "tooling", "tools", "toolkit", "eks", "kubernetes", "k8s",
        "ecs", "container", "registry", "ecr", "terraform", "automation", "infra", "devflow",
        "devinsights", "devcloud", "awstesting", "client-test", "forge", "foundry",
        "dogfooding", "canary", "spinup", "opensource", "cloudops", "aws-osb", "osb",
        "cloudasaservice", "cloud-", "-cloud", "azure", "robotic", "experiencecreation",
        "releaseruntime", "orbit", "optimus", "heracles", "axon", "enablement", "developer",
    ]),
    ("Payments & Cards", [
        "payment", "pay-", "paykit", "card", "issuer", "issuing", "acquir", "clearing",
        "settlement", "switch", "authoriz", "tokeniz", "token", "wallet", "stablecoin",
        "crypto", "blockchain", "remit", "transfer", "billpay", "xborder", "swift",
        "interchange", "transaction", "mpgs", "orderprocessing", "mdes", "tiplus",
        "openfinance", "rtp", "protocolconnect", "spei", "acs-", "-acs", "nextgenpoi",
        "commercial", "bulkpayment", "filetransfer", "funds", "remittance", "sdram", "ipscore",
    ]),
    ("Fraud & Risk", [
        "fraud", "risk", "aml", "kyc", "sanction", "decision", "scoring", "dispute",
        "detectandidentify", "brighterion",
    ]),
    ("Loyalty, Offers & Marketing", [
        "loyalty", "offers", "offer", "shopper", "marketplace", "market", "media",
        "attribution", "leads", "tourism", "spendingpulse", "smallbusiness", "merchant",
        "martech", "commerce", "carbon", "benefit", "rewards", "campaign", "moments",
    ]),
    ("Customer & Digital", [
        "customer", "digital", "portal", "web", "mobile", "app-", "api", "gateway",
        "consumer", "partner", "crm", "humanresources", "workplace", "operational",
        "servicecatalog", "localmi", "edie", "kmp", "andaplad", "meitech", "co-", "-co",
    ]),
    ("Sandbox & Experiments", [
        "sandbox", "sbx", "poc", "playground", "experiment", "-lab", "lab-", "learninglab",
        "innovation", "hackathon", "trial", "demo", "training", "learn", "testandlearn",
        "msbx",
    ]),
]
# Words removed before matching: the company prefix says nothing about the account
# ("mastercard" would otherwise read as "master" and "card").
NOISE = re.compile(r"^(mc-|mastercard-?)|mastercard")
OTHER = "Other accounts"

# The environment is the name's last part (an account number may follow it).
ENVIRONMENTS = [
    ("Non-prod", ["nonprod", "non-prod", "nonprd", "nonp", "np", "lower"]),
    ("Prod", ["prod", "production", "prd", "live", "prdn"]),
    ("Stage", ["stage", "staging", "stg", "preprod", "pre-prod", "pre", "uat", "mtf", "perf", "cert"]),
    ("Test", ["test", "testing", "tst", "qa", "sit", "int", "integration"]),
    ("Dev", ["dev", "develop", "development", "work"]),
    ("Sandbox", ["sandbox", "sbx", "lab", "poc", "demo"]),
    ("DR", ["dr", "disasterrecovery"]),
]
ENVIRONMENT_ORDER = [name for name, _ in ENVIRONMENTS] + ["Other"]
ROLE_ARN = re.compile(r"arn:aws[\w-]*:iam::(\d{12}):role/(.+)")


def default_path():
    return os.environ.get("AWS_CONFIG_FILE") or str(Path("~/.aws/config").expanduser())


def environment(profile):
    """(environment, name without it) from the profile's last part."""
    parts = re.split(r"[-_.]", profile.lower())
    if parts and parts[-1].isdigit() and len(parts) > 1:
        parts = parts[:-1]
    joined = "-".join(parts)
    for name, words in ENVIRONMENTS:
        for word in sorted(words, key=len, reverse=True):
            if joined == word or joined.endswith("-" + word):
                return name, joined[: -len(word)].rstrip("-") or joined
    return "Other", joined


def category(profile):
    _, base = environment(profile)
    base = NOISE.sub("", base)
    # "-" marks the name's edges, so rules like "-db" or "co-" match whole parts only.
    edged = f"-{base}-"
    for name, words in CATEGORY_RULES:
        if any(word in edged for word in words):
            return name
    return OTHER


def read(path):
    """Accounts in the AWS config file at path. Raises FileNotFoundError or ValueError."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        with open(path, encoding="utf-8-sig") as file:
            parser.read_file(file)
    except configparser.Error as error:
        raise ValueError(f"The AWS config file could not be read: {error.message.splitlines()[0]}") from None
    sessions = {
        section[len("sso-session "):]: dict(parser.items(section))
        for section in parser.sections()
        if section.startswith("sso-session ")
    }
    accounts = []
    for section in parser.sections():
        if section.startswith("profile "):
            profile = section[len("profile "):].strip()
        elif section == "default":
            profile = "default"
        else:
            continue
        values = dict(parser.items(section))
        account_id = values.get("sso_account_id", "")
        role = values.get("sso_role_name", "")
        kubernetes_role = values.get("kubernetes_role_name", "")
        match = ROLE_ARN.match(values.get("role_arn", ""))
        if match:
            account_id = account_id or match.group(1)
            role = role or match.group(2)
        session = sessions.get(values.get("sso_session", ""), {})
        env, _ = environment(profile)
        accounts.append({
            "profile": profile,
            "account_id": account_id,
            "role": role,
            "kubernetes_role": kubernetes_role,
            "region": values.get("region", "") or values.get("sso_region", "") or session.get("sso_region", ""),
            "sso_start_url": values.get("sso_start_url", "") or session.get("sso_start_url", ""),
            "environment": env,
        })
    return accounts


def inventory(path):
    """The accounts page's data: categories of accounts, ordered by environment."""
    accounts = read(path)
    categories = {}
    for account in sorted(
        accounts,
        key=lambda item: (ENVIRONMENT_ORDER.index(item["environment"]), item["profile"]),
    ):
        categories.setdefault(category(account["profile"]), []).append(account)
    # The roles to copy: the most used AWS (SSO) role and Kubernetes role, then any
    # other AWS role that several accounts use.
    aws_roles = Counter(account["role"] for account in accounts if account["role"])
    kubernetes_roles = Counter(account["kubernetes_role"] for account in accounts if account["kubernetes_role"])
    common = {}
    if aws_roles:
        common["aws_role"] = aws_roles.most_common(1)[0][0]
    if kubernetes_roles:
        common["kubernetes_role"] = kubernetes_roles.most_common(1)[0][0]
    for index, (role, count) in enumerate(aws_roles.most_common()[1:4], 2):
        if count > 1:
            common[f"aws_role_{index}"] = role
    return {
        "source": "aws-config",
        "total_accounts": len(accounts),
        "common_roles": common,
        "categories": dict(sorted(categories.items(), key=lambda item: (item[0] == OTHER, item[0]))),
        "environments": ENVIRONMENT_ORDER,
    }
