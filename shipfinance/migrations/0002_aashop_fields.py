"""Add aa-shop order fields to FinanceAgreement for shop installment plans."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipfinance", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="financeagreement",
            name="aashop_order_id",
            field=models.BigIntegerField(
                blank=True, null=True,
                help_text="aa-shop order ID if this is an installment plan for a shop order."),
        ),
        migrations.AddField(
            model_name="financeagreement",
            name="aashop_order_reference",
            field=models.CharField(
                blank=True, default="", max_length=10,
                help_text="aa-shop order reference (e.g. ABC12) for display."),
        ),
        migrations.AddField(
            model_name="financeagreement",
            name="aashop_item_summary",
            field=models.CharField(
                blank=True, default="", max_length=200,
                help_text="Item summary from the aa-shop order (for display)."),
        ),
    ]
