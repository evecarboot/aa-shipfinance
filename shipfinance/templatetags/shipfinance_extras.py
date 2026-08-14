"""Template tags for shipfinance."""
from django import template

register = template.Library()


@register.filter
def isk(value):
    """Format an ISK amount with thousands separators."""
    try:
        return f"{float(value):,.2f}"
    except (ValueError, TypeError):
        return value
