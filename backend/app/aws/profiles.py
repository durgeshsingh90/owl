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
        "centralized", "centralised", "central", "shared", "sharedsvc", "sharedservice",
        "common", "coreservice", "core-services", "hub", "landingzone", "landing-zone",
        "directory", "activedirectory", "mgmt-shared", "tooling-shared",
    ]),
    ("Management & Billing", [
        "management", "mgmt", "master", "organization", "organisation", "payer",
        "billing", "orgroot", "root-account", "controltower", "control-tower", "governance",
    ]),
    ("Security", [
        "security", "secops", "infosec", "cyber", "guardduty", "securityhub", "audit",
        "siem", "kms", "hsm", "pki", "certificate", "secrets", "vault",
        "stablecoinsecurity", "compliance", "forensic", "vulnerab", "pentest", "waf-sec",
    ]),
    ("Identity & Access", [
        "identity", "-iam", "iam-", "idp", "-sso", "sso-", "okta", "pingfed", "ping-",
        "cognito", "authn", "authentication", "access",
    ]),
    ("Networking", [
        "network", "networking", "egress", "ingress", "palo", "paloalto", "firewall",
        "fw-", "-fw", "vpc", "transit", "tgw", "dns", "route53", "akamai", "cdn",
        "cloudfront", "natgw", "nat-gateway", "proxy", "waf", "vpn", "directconnect", "dx-", "loadbalanc",
        "-elb", "edge", "connectivity", "peering", "subnet", "ipam",
    ]),
    ("Logging & Monitoring", [
        "logarchive", "log-archive", "logging", "logs", "log-", "monitor", "observab",
        "splunk", "datadog", "dynatrace", "grafana", "prometheus", "cloudwatch",
        "newrelic", "elastic", "opensearch", "telemetry", "apm",
    ]),
    ("Backup & DR", [
        "backup", "disaster", "recovery", "-dr", "dr-", "resilien", "failover",
    ]),
    ("Data & Analytics", [
        "data", "analytics", "datalake", "lake", "warehouse", "lakehouse", "etl", "glue",
        "redshift", "snowflake", "emr", "databricks", "athena", "bi-", "reporting",
        "machinelearning", "mlops", "sagemaker", "ai-", "genai", "bedrock",
    ]),
    ("Streaming & Messaging", [
        "kafka", "msk", "stream", "kinesis", "messag", "queue", "sqs", "sns", "eventbus",
        "eventbridge", "rabbitmq", "activemq",
    ]),
    ("Databases", [
        "database", "db-", "-db", "rds", "aurora", "dynamo", "postgres", "mysql",
        "oracle", "mongo", "redis", "cache",
    ]),
    ("Platform & DevOps", [
        "platform", "devops", "cicd", "ci-cd", "pipeline", "build", "deploy", "jenkins",
        "artifactory", "nexus", "tooling", "tools", "eks", "kubernetes", "k8s", "ecs",
        "container", "registry", "ecr", "terraform", "automation", "infra",
    ]),
    ("Payments & Cards", [
        "payment", "payments", "pay-", "card", "issuer", "issuing", "acquir", "clearing",
        "settlement", "switch", "authoriz", "tokeniz", "token", "wallet", "stablecoin",
        "crypto", "blockchain", "remit", "transfer", "billpay",
    ]),
    ("Fraud & Risk", [
        "fraud", "risk", "aml", "kyc", "sanction", "decision", "scoring",
    ]),
    ("Customer & Digital", [
        "customer", "digital", "portal", "web", "mobile", "app-", "api", "gateway",
        "consumer", "merchant", "partner", "crm",
    ]),
    ("Sandbox & Experiments", [
        "sandbox", "sbx", "poc", "playground", "experiment", "-lab", "lab-", "innovation",
        "hackathon", "trial", "demo", "training", "learn",
    ]),
]
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
    # Without the common "mc-" style prefix the rest of the name decides.
    for name, words in CATEGORY_RULES:
        if any(word in base for word in words):
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
    roles = Counter(account["role"] for account in accounts if account["role"])
    return {
        "source": "aws-config",
        "total_accounts": len(accounts),
        "common_roles": {role: role for role, _ in roles.most_common(6)},
        "categories": dict(sorted(categories.items(), key=lambda item: (item[0] == OTHER, item[0]))),
        "environments": ENVIRONMENT_ORDER,
    }
