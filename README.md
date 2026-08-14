# aa-shipfinance

An AllianceAuth 5.0 plugin to **rent and finance doctrine ships** to trusted corp members. Members can rent fitted ships for short periods, or finance-purchase them with monthly installments and interest. Payments are collected automatically via the [allianceauth-invoices](https://github.com/Solar-Helix-Independent-Transport/allianceauth-invoice-manager) plugin.

Inspired by real-world rent-a-car and car-finance programs — but for EVE Online ships. Intended as a fun, trust-based plugin for corps that want to help members fly good ships before they can afford them outright.

---

## Features

### Rent
- Members rent a fitted doctrine ship for a period (hours/days/weeks).
- Two ways to rent:
  - **Contract / hangar request** (form-based): member picks a ship, chooses delivery mode, duration, and billing period, acknowledges terms. Plugin creates an invoice upfront.
  - **Free-use hangar** (automatic, no form): admin sets a per-fit free-use rate. Member just takes the ship from the free-use hangar in-game. Plugin auto-detects the ship leaving the corp hangar, creates a rental, and bills for actual time used when the ship is returned to any corp hangar.
- ESI asset polling detects when the ship is returned to the corp hangar.
- zKillboard/ESI killmails detect if the ship is destroyed.
- Rental fee invoiced via the invoices plugin.

### Finance
- Members receive a ship **up front** and pay it off in monthly installments.
- Admin-configurable interest: **flat add-on** (simple, predictable) or **APR** (declining balance, rewards early payoff).
- Optional **insurance** at signing: if the ship is destroyed, insurance settles the remaining balance. Without insurance, the member still owes the full remaining balance (like real car finance).
- Installment schedule auto-generated as monthly invoices via the invoices plugin.

### Trust model
- **Request-only access group**: admins curate who can use the plugin.
- **No-overdue secure group filter**: members with overdue invoices auto-lose access (`NoOverdueShipFinanceFilter`).
- **Admin-managed defaults**: no automatic kicks. Admins decide how to handle arrears, losses, and defaults per case.
- **Audit log**: every state change is logged for dispute resolution.

---

## Installation

### Requirements
- AllianceAuth 5.0+
- [allianceauth-corptools](https://github.com/Solar-Helix-Independent-Transport/allianceauth-corp-tools) (recommended for fast asset lookups; plugin falls back to direct ESI if not installed)
- [allianceauth-invoices](https://github.com/Solar-Helix-Independent-Transport/allianceauth-invoice-manager) v0.1.9+ (for payment collection)
- Optional: [allianceauth-georgeforge](https://pypi.org/project/allianceauth-georgeforge/) (for automated contract delivery)

### ESI scopes required
The plugin uses ESI directly for asset tracking. Ensure your corp has tokens with these scopes:
- `esi-assets.read_corporation_assets.v1` (requires Director role) — for corp asset polling
- `esi-assets.read_assets.v1` — for character asset lookups (free-use detection)

If corp-tools is installed, the plugin uses corp-tools' cached asset data (faster). If not, it falls back to direct ESI calls.

### Steps

1. Install the package:
   ```bash
   pip install aa-shipfinance
   ```

2. Add `'shipfinance'` to your `INSTALLED_APPS` in `local.py`:
   ```python
   INSTALLED_APPS += [
       "shipfinance",
   ]
   ```

3. Run migrations, collect static, and restart:
   ```bash
   python manage.py migrate shipfinance
   python manage.py collectstatic
   supervisorctl restart all
   ```

4. Set up the default periodic tasks (asset polling, destroyed detection, payoff checks):
   ```bash
   python manage.py shipfinance_setup_tasks
   ```
   Or visit the admin dashboard and click "Setup Periodic Tasks".

5. Configure permissions (see below).

6. Set up your doctrine fits, register ship stock, and create finance offers via the admin UI.

---

## Configuration

All settings are optional and have sensible defaults. Add to your `local.py`:

```python
## Ship Finance settings

# Display name in the Auth sidebar
SHIPFINANCE_APP_NAME = "Ship Finance"

# Payment corp ID (falls back to invoices' PAYMENT_CORP if not set)
SHIPFINANCE_PAYMENT_CORP = 123456789

# Invoice ref prefix (keep opaque for op-sec; appears in wallet journal)
SHIPFINANCE_INVOICE_REF_PREFIX = "SF"

# Default interest type for new offers: "flat" or "apr"
SHIPFINANCE_DEFAULT_INTEREST_TYPE = "flat"

# Default interest rate (percentage)
SHIPFINANCE_DEFAULT_INTEREST_RATE = 10

# Default insurance premium rate (% of principal)
SHIPFINANCE_DEFAULT_INSURANCE_PREMIUM_RATE = 5

# Default insurance coverage: "remaining_balance", "principal", "flat_amount"
SHIPFINANCE_DEFAULT_INSURANCE_COVERAGE = "remaining_balance"

# Refunds on default (False = no refunds, default)
SHIPFINANCE_REFUNDS_ALLOWED = False

# Default billing period for free-use rentals: "hourly", "daily", "weekly"
SHIPFINANCE_DEFAULT_BILLING_PERIOD = "daily"

# Asset poll cadence (minutes) for return/destroyed detection
SHIPFINANCE_ASSET_POLL_MINUTES = 60

# Enable zKillboard API fallback for destroyed-ship detection
SHIPFINANCE_ZKILL_FALLBACK = True

# Notifications
SHIPFINANCE_SEND_AUTH_NOTIFICATIONS = True
SHIPFINANCE_SEND_DISCORD_NOTIFICATIONS = True
```

---

## Permissions

| Perm | Description |
|------|-------------|
| `shipfinance.access_shipfinance` | Can access the Ship Finance app (see menu) |
| `shipfinance.manage_shipfinance` | Admin: manage fits, stock, offers, agreements |
| `shipfinance.use_rent` | Member: can rent ships |
| `shipfinance.use_finance` | Member: can finance ships |

Assign `access_shipfinance` + `use_rent`/`use_finance` to your trusted request-only group. Attach the `NoOverdueShipFinanceFilter` secure group filter to auto-lockout members with overdue invoices.

---

## Usage

### Admins

1. **Create doctrine fits** (`/shipfinance/admin/fits/`): define the hull, fitting (DNA string), skill tier label, and free-use hangar rate (if you want free-use rentals for this fit).
2. **Register ship stock** (`/shipfinance/admin/stock/`): add each assembled ship by its ESI `item_id`, location, and hangar division.
3. **Designate free-use hangars** (`/shipfinance/admin/hangars/`): mark which corp hangar divisions (at which locations) are free-use. Ships in these hangars with a non-zero free-use rate are auto-rented when taken.
3. **Create finance offers** (`/shipfinance/admin/offers/`): define principal, term, interest, and insurance terms for each fit.
4. **Monitor agreements** (`/shipfinance/admin/rentals/` and `/shipfinance/admin/finances/`): mark ships returned, destroyed, lost, or finances paid off / defaulted.
5. **Review audit log** (`/shipfinance/admin/audit/`): every action is logged.

### Members

1. **Browse** available ships and finance offers (`/shipfinance/browse/`).
2. **Rent** a ship: choose delivery mode, duration, acknowledge terms.
3. **Finance** a ship: review the payment schedule, optionally buy insurance, acknowledge terms.
4. **Pay invoices**: use the invoices plugin — transfer ISK to the corp wallet with the invoice ref as the reason.
5. **Track** your rentals and finances (`/shipfinance/my-rentals/` and `/shipfinance/my-finances/`).

---

## How tracking works

### Ship tracking (op-sec note)
Ships are tracked by their **ESI `item_id`** — a stable identifier for the life of the assembled ship. **Ship names are NOT used for tracking** and should not be set to anything that reveals the rental program (e.g. do not name ships `SF-RENT-123`). Members can rename ships freely; tracking is unaffected.

### Return detection
A periodic task polls corp assets via **ESI** (`/corporations/{corp_id}/assets/`) directly, falling back to corp-tools if ESI is unavailable. If a rented ship's `item_id` reappears in its expected corp hangar division at its home location, the rental is marked returned. For free-use rentals, the ship can be returned to **any** corp hangar in any station.

### Destroyed detection
If a rented/financed ship is no longer in the corp hangar or the member's assets, the plugin checks the **zKillboard API** for a recent loss matching the hull type and member character. If found, the agreement is closed as destroyed. zKill matching is best-effort (see limitations below).

### Limitations (be aware)
- **Repackaging destroys the `item_id`**: if a member repackages the ship, tracking is lost. This is a trust issue, not a plugin issue — the request-only group mitigates it.
- **Poll cadence bounds accuracy**: if assets are polled hourly, return/billing detection is accurate to ~1 hour. Configure via `SHIPFINANCE_ASSET_POLL_MINUTES`.
- **Take-and-return between polls is invisible**: in free-use mode, if a member takes and returns a ship within one poll cycle, the plugin may not see it. This is a known trade-off of the free-use model. Configure `SHIPFINANCE_ASSET_POLL_MINUTES` to balance accuracy vs ESI load.
- **Free-use ships can be in any station**: the plugin tracks ships by ESI `item_id`, not by location. A ship taken from a free-use hangar in one station can be returned to any corp hangar in any station — the return task checks corp assets globally, not just the home hangar.
- **zKill matching is best-effort**: matches on hull type + victim character + recency. It does not use `item_id` (zKill doesn't expose it). False positives are possible but unlikely for doctrine hulls.

---

## Trust model & defaults

This plugin is designed for **trusted, request-only groups**. It is not a theft-detection or enforcement system. Specifically:

- **No theft detection**: if a trusted member sells/contracts a rented ship to an alt, the plugin reports "not returned" and an admin decides what to do.
- **No automatic kicks**: arrears flag the account via the `NoOverdueShipFinanceFilter`, but removing the member is an admin decision.
- **No role automation**: for free-use hangar mode, the plugin does not manage EVE corp roles. Admins grant/revoke hangar access manually.
- **Admin-managed defaults**: when a member stops paying or a ship goes missing, admin actions (`mark_defaulted`, `mark_lost`, `mark_destroyed`) handle it case by case.

If you want hard enforcement, this is the wrong plugin. If you want a fun, low-friction way to let trusted members fly good ships and pay over time, this is it.

---

## GeorgeForge integration (optional)

If [allianceauth-georgeforge](https://pypi.org/project/allianceauth-georgeforge/) is installed, the plugin will attempt to create delivery contracts via GeorgeForge when a ship is rented or financed. If GeorgeForge is not installed, delivery is a manual admin step ("mark as contracted").

The GeorgeForge integration is a **stub** — implement `create_delivery_contract()` in `shipfinance/georgeforge_integration.py` for your GF version.

---

## Destroyed-ship finance policy

When a financed ship is destroyed:

- **With insurance**: the insurance payout settles the remaining balance (fully or partially, depending on coverage mode). The finance is closed.
- **Without insurance**: the member **still owes the full remaining balance**. This mirrors real car finance — the debt survives the asset. The admin can override per case (forgive the balance, convert to ISK debt) via the `mark_defaulted` action.

This policy is shown to members in the terms text before they accept a finance offer, so there are no surprises.

---

## Development

### Running tests
```bash
pip install -e ".[tests]"
pytest tests/
```

### Package structure
```
shipfinance/
  __init__.py              # version, title
  apps.py                  # AppConfig
  app_settings.py          # settings + feature detection
  models.py                # DoctrineFit, ShipStock, RentalAgreement, FinanceAgreement, etc.
  managers.py              # visible_to querysets
  helpers.py               # interest calc, insurance, invoice creation, state transitions
  tasks.py                 # Celery tasks: asset polling, destroyed detection, payoffs
  georgeforge_integration.py  # optional GF hook (stub)
  views.py                 # member + admin views
  urls.py                  # URL routing
  admin.py                 # Django admin registration
  auth_hooks.py            # menu item, URL hook, secure group filter
  migrations/
  templates/shipfinance/   # all templates
  templatetags/
  management/commands/
tests/                     # test suite (settings, test cases, fakeredis plugin)
```

---

## License

MIT — see [LICENSE](LICENSE).
