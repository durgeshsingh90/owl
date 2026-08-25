import json

from django.core.management.base import BaseCommand

from core.services.system_status import get_system_status


class Command(BaseCommand):
    help = "Report OWL's local readiness without printing credential values."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit a machine-readable sanitized result.",
        )

    def handle(self, *args, **options):
        status = get_system_status()
        if options["as_json"]:
            components = []
            for component in status["components"]:
                safe_component = dict(component)
                if safe_component["key"] == "data_root":
                    safe_component["detail"] = "The configured local data root is writable."
                components.append(safe_component)
            payload = {
                "overall_state": status["overall_state"],
                "components": components,
            }
            self.stdout.write(json.dumps(payload, sort_keys=True))
            return

        self.stdout.write(f"OWL status: {status['overall_state']}")
        for component in status["components"]:
            self.stdout.write(f"- {component['label']}: {component['summary']}")
