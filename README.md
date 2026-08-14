# aa-shipfinance

An AllianceAuth 5.0+ plugin to **rent and finance doctrine ships** to trusted corp members. Members can rent fitted ships for short periods (hourly/daily/weekly), or finance-purchase them with monthly installments and interest. Payments are collected automatically via the [allianceauth-invoices](https://github.com/Solar-Helix-Independent-Transport/allianceauth-invoice-manager) plugin.

Inspired by real-world rent-a-car and car-finance programs — but for EVE Online ships. Intended as a fun, trust-based plugin for corps that want to help members fly good ships before they can afford them outright.

---

## Features

### Rentals

Two ways to rent ships to members:

#### Contract / hangar request (form-based)

- Admin configures rental rates per doctrine fit: **hourly, daily, and weekly** (set any to 0 to disable that period)
- Member picks a ship, chooses a billing period from the admin-configured options, enters duration in hours, acknowledges terms
- The member **cannot set the price** — rates come from admin configuration
- Invoice created upfront for the full rental fee
- Example: admin sets 5M ISK/hour on a Procurer fit. Member rents for 4 hours of moon mining → billed 20M ISK

#### Self-service rental hangar (automatic, no form)

- Admin designates a **corp hangar division** as a rental hangar (e.g. Division 3)
- Admin sets a **ship name prefix** (e.g. `.BANR`)
- Admin names ships in-game with the prefix (e.g. `.BANR Gila 1`, `.BANR Proc 2`) and places them in the designated division
- The division is rental at **any station** — not tied to one location
- Member takes a ship from the division in-game — the rental starts automatically
- Plugin auto-discovers prefixed ships by scanning corp assets (no manual stock registration needed)
- Ships in the division without the prefix are ignored (random stuff is safe)
- When the ship is returned to a corp hangar, the member is billed for **actual time used**
- Optional **return-to-origin** requirement: if enabled, the rental only closes when the ship is returned to the **same station** it was rented from. Returning to a different station keeps the rental open and billing continues until the ship is back at the origin

### Finance

- Members receive a ship **up front** and pay it off in monthly installments
- Admin-configurable interest: **flat add-on** (simple, predictable) or **APR** (declining balance, rewards early payoff)
- Optional **insurance** at signing: if the ship is destroyed, insurance settles the remaining balance. Without insurance, the member still owes the full remaining balance (like real car finance)
- Installment schedule auto-generated as monthly invoices via the invoices plugin

### GeorgeForge installment plans (optional)

If [allianceauth-georgeforge](https://pypi.org/project/allianceauth-georgeforge/) is installed, members can **split the full cost of their GeorgeForge ship orders into monthly installments** instead of paying upfront.

GeorgeForge is a card builder tool where admins list ships with prices and optional deposits. Normally members pay upfront at checkout. With this plugin, members can instead:

1. Place an order in GeorgeForge (order status = Awaiting Deposit)
2. Come to this plugin and select "GeorgeForge Installment Plans"
3. Choose a payment plan (term + interest rate) and acknowledge terms
4. The original GeorgeForge deposit invoice is cancelled
5. This plugin creates monthly installment invoices via the invoices plugin for the **full order cost** (including any deposit)
6. When all installments are paid, the GeorgeForge order automatically advances to "Deposit Received" and the ship gets built
7. The member receives the ship from GeorgeForge when it's delivered

The ship is only built once the full order cost is paid off through installments. If the order has a deposit, it's just part of the total being financed — the member pays everything off in installments, then the ship gets built.

### Trust model

- **Request-only access group**: admins curate who can use the plugin
- **No-overdue secure group filter**: members with overdue invoices auto-lose access (`NoOverdueShipFinanceFilter`)
- **Admin-managed defaults**: no automatic kicks. Admins decide how to handle arrears, losses, and defaults per case
- **Audit log**: every state change is logged for dispute resolution

---

## Installation

### Requirements

- AllianceAuth 5.0+
- [allianceauth-corptools](https://github.com/Solar-Helix-Independent-Transport/allianceauth-corp-tools) (recommended for fast asset lookups; plugin falls back to direct ESI if not installed)
- [allianceauth-invoices](https://github.com/Solar-Helix-Independent-Transport/allianceauth-invoice-manager) v0.1.9+ (for payment collection)
- Optional: [allianceauth-georgeforge](https://pypi.org/project/allianceauth-georgeforge/) (for installment plans on ship orders)

### ESI scopes required

The plugin uses ESI directly for asset tracking. Ensure your corp has tokens with these scopes:

- `esi-assets.read_corporation_assets.v1` (requires Director role) — for corp asset polling
- `esi-assets.read_assets.v1` — for character asset lookups (self-service rental detection)

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

6. Set up your doctrine fits, rental hangars, and finance offers via the admin UI.

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

# Default billing period for self-service rentals: "hourly", "daily", "weekly"
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

1. **Create doctrine fits** (`/shipfinance/admin/fits/`): define the hull, fitting (DNA string), skill tier label, rental rates (hourly/daily/weekly), and self-service rental rate (if you want auto-rentals for this fit).
2. **Designate rental hangars** (`/shipfinance/admin/hangars/`): mark which corp hangar division(s) are rental hangars. Set a ship name prefix (e.g. `.BANR`). Optionally enable "require return to origin station".
3. **Register ship stock** (`/shipfinance/admin/stock/`): add each assembled ship by its ESI `item_id`, location, and hangar division. (Ships in rental hangars with the correct name prefix are auto-registered — you don't need to do this manually for self-service rentals.)
4. **Create finance offers** (`/shipfinance/admin/offers/`): define principal, term, interest, and insurance terms for each fit. These offers are also used for GeorgeForge deposit installment plans.
5. **Monitor agreements** (`/shipfinance/admin/rentals/` and `/shipfinance/admin/finances/`): mark ships returned, destroyed, lost, or finances paid off / defaulted.
6. **Review audit log** (`/shipfinance/admin/audit/`): every action is logged.

### Members

1. **Browse** available ships and finance offers (`/shipfinance/browse/`). Rental rates are shown per fit.
2. **Rent** a ship: choose delivery mode, billing period (from admin-configured options), duration, acknowledge terms. The price is set by the admin — you just pick how long.
3. **Self-service rental**: just take a ship from the rental hangar division in-game. No form needed. Return it to a corp hangar to stop billing.
4. **Finance** a ship: review the payment schedule, optionally buy insurance, acknowledge terms.
5. **GeorgeForge installment plans** (if GF is installed): split the full cost of your GeorgeForge ship order into monthly payments instead of paying upfront. The ship gets built once it's fully paid off.
6. **Pay invoices**: use the invoices plugin — transfer ISK to the corp wallet with the invoice ref as the reason.
7. **Track** your rentals and finances (`/shipfinance/my-rentals/` and `/shipfinance/my-finances/`).

---

## How tracking works

### Self-service rental detection

The plugin scans corp assets in the designated rental division(s) for ships whose name starts with the configured prefix. It uses corptools' `CorpAsset` model (which caches the `name` field) when available, or falls back to the ESI corporation assets + asset names endpoints.

When a prefixed ship is found in the division, a `ShipStock` record is auto-created (matched to a `DoctrineFit` by hull type ID for pricing). When the ship leaves the division, the plugin searches member character assets for the ship's `item_id` to identify who took it, and auto-creates a rental.

### Ship tracking (op-sec note)

Ships are tracked by their **ESI `item_id`** — a stable identifier for the life of the assembled ship. Ship names are only used for the self-service prefix detection; they are not used for ongoing tracking. Invoice references use opaque codes (e.g. `SF-XXXXXXXX`) and do not reveal rental/finance details.

### Return detection

A periodic task polls corp assets via ESI (falling back to corp-tools cached data). If a rented ship's `item_id` reappears in corp assets, the rental is marked returned. For self-service rentals, the ship can be returned to any corp hangar — unless "require return to origin" is enabled, in which case it must be at the same station it was rented from.

### Destroyed detection

If a rented/financed ship is no longer in the corp hangar or the member's assets, the plugin checks the **zKillboard API** for a recent loss matching the hull type and member character. If found, the agreement is closed as destroyed. zKill matching is best-effort (see limitations below).

### Limitations (be aware)

- **Repackaging destroys the `item_id`**: if a member repackages the ship, tracking is lost. This is a trust issue, not a plugin issue — the request-only group mitigates it.
- **Poll cadence bounds accuracy**: if assets are polled hourly, return/billing detection is accurate to ~1 hour. Configure via `SHIPFINANCE_ASSET_POLL_MINUTES`.
- **Take-and-return between polls is invisible**: in self-service mode, if a member takes and returns a ship within one poll cycle, the plugin may not see it. Configure `SHIPFINANCE_ASSET_POLL_MINUTES` to balance accuracy vs ESI load.
- **zKill matching is best-effort**: matches on hull type + victim character + recency. It does not use `item_id` (zKill doesn't expose it). False positives are possible but unlikely for doctrine hulls.

---

## Trust model & defaults

This plugin is designed for **trusted, request-only groups**. It is not a theft-detection or enforcement system. Specifically:

- **No theft detection**: if a trusted member sells/contracts a rented ship to an alt, the plugin reports "not returned" and an admin decides what to do.
- **No automatic kicks**: arrears flag the account via the `NoOverdueShipFinanceFilter`, but removing the member is an admin decision.
- **No role automation**: the plugin does not manage EVE corp roles. Admins grant/revoke hangar access manually.
- **Admin-managed defaults**: when a member stops paying or a ship goes missing, admin actions (`mark_defaulted`, `mark_lost`, `mark_destroyed`) handle it case by case.

If you want hard enforcement, this is the wrong plugin. If you want a fun, low-friction way to let trusted members fly good ships and pay over time, this is it.

---

## GeorgeForge integration (optional)

If [allianceauth-georgeforge](https://pypi.org/project/allianceauth-georgeforge/) is installed, this plugin adds an **installment plan** option to GeorgeForge orders.

GeorgeForge is a card builder tool where admins list ships with prices and optional deposits. Members place orders and normally pay upfront at checkout. With this plugin installed, members can instead split the **full order cost** into monthly installments:

1. Member places an order in GeorgeForge → order status = Awaiting Deposit
2. Member visits this plugin → "GeorgeForge Installment Plans"
3. Member sees their GF orders awaiting payment
4. Member picks a payment plan (term + interest rate from your configured finance offers)
5. The original GF deposit invoice is cancelled
6. This plugin creates monthly installment invoices for the full order cost (including any deposit)
7. When all installments are paid → the GF order automatically advances to "Deposit Received" and the ship gets built
8. GeorgeForge continues its normal build/delivery flow → member gets the ship when it's delivered

The ship is only built once the full order cost is paid off. If the order has a deposit, it's just part of the total — the member pays everything off in installments, then the ship gets built.

If GeorgeForge is not installed, this feature is simply not available — no errors, no broken behavior.

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
  __init__.py                     # version, title
  apps.py                         # AppConfig
  app_settings.py                 # settings + feature detection
  models.py                       # DoctrineFit, ShipStock, RentalAgreement, FinanceAgreement, etc.
  managers.py                     # visible_to querysets
  helpers.py                      # interest calc, insurance, invoice creation, state transitions
  tasks.py                        # Celery tasks: asset polling, rental detection, destroyed detection
  georgeforge_integration.py      # optional GeorgeForge deposit installment integration
  views.py                        # member + admin views
  urls.py                         # URL routing
  admin.py                        # Django admin registration
  auth_hooks.py                   # menu item, URL hook, secure group filter
  migrations/
  templates/shipfinance/          # Bootstrap 5 templates
  templatetags/
  management/commands/
```

---

## License

MIT — see [LICENSE](LICENSE).
