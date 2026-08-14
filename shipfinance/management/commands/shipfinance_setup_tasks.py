"""Management command to set up default periodic tasks."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or update the default periodic tasks for shipfinance."

    def handle(self, *args, **options):
        from shipfinance.tasks import setup_default_tasks
        setup_default_tasks.apply_async()
        self.stdout.write(self.style.SUCCESS(
            "Ship Finance default periodic tasks created/updated."))
