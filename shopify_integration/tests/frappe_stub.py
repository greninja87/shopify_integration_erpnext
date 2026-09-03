"""
frappe_stub.py — a recording `frappe` fake, so this app's logic can be unit
tested without a bench.

The repo has no bench test harness.  Rather than skip tests, we install a fake
`frappe` into sys.modules before the module under test is imported.  The fake
records every write, which lets a test assert not only what was written but
also — just as important here — what was *not*.

Usage:

    from shopify_integration.tests.frappe_stub import install, reset

    frappe = install()          # idempotent; safe to call from every module
    ...
    reset()                     # in setUp, to clear recorded state

Only the surface this app actually touches is implemented.  Anything else is a
no-op returning None, which surfaces as a normal AttributeError/TypeError in
the test rather than a silent pass.
"""

import datetime
import sys
import types

# doctype -> {docname: {fieldname: value}}
DB = {}
# every frappe.db.set_value call, in order: (doctype, name, values, kwargs)
WRITES = []
# every frappe.log_error call: (message, title)
ERRORS = []
# rows returned by the next frappe.db.sql call, keyed by a substring of the query
SQL_RESULTS = {}
# fields present on each doctype's meta
META_FIELDS = {"Payment Entry": set()}
COMMITS = []
# (doctype, docname, fieldname) -> plaintext, read back by get_decrypted_password
PASSWORDS = {}
# key -> value, standing in for frappe.cache()
CACHE = {}
# key -> expires_in_sec passed to set_value, so a test can assert the TTL
CACHE_TTL = {}
# doctype -> set of column names frappe.db.has_column() should report as present
COLUMNS = {}
# every doc dict inserted via frappe.get_doc({...}).insert(), in order
INSERTS = []


def reset():
    """Clear all recorded state.  Call from setUp()."""
    DB.clear()
    WRITES.clear()
    ERRORS.clear()
    SQL_RESULTS.clear()
    COMMITS.clear()
    PASSWORDS.clear()
    CACHE.clear()
    CACHE_TTL.clear()
    COLUMNS.clear()
    COLUMNS["Address"] = {"gstin", "gst_category"}
    INSERTS.clear()
    META_FIELDS.clear()
    META_FIELDS["Payment Entry"] = {
        "custom_gateway_reference",
        "custom_gateway_name",
        "reference_no",
        "reference_date",
    }


def set_doc(doctype, name, values):
    """Seed a document into the fake DB."""
    DB.setdefault(doctype, {})[name] = dict(values)


def get_doc_values(doctype, name):
    return dict(DB.get(doctype, {}).get(name, {}))


def _add_to_date(date=None, years=0, months=0, weeks=0, days=0, hours=0,
                 minutes=0, seconds=0, as_string=False, as_datetime=False):
    """Enough of frappe.utils.add_to_date for the offsets this app uses."""
    base = date or datetime.datetime(2026, 8, 26, 12, 0, 0)
    if isinstance(base, str):
        base = datetime.datetime.fromisoformat(base)
    return base + datetime.timedelta(
        weeks=weeks, days=days + years * 365 + months * 30,
        hours=hours, minutes=minutes, seconds=seconds,
    )


class _Cache:
    """Stand-in for frappe.cache(); records TTLs but does not expire on its own."""

    def set_value(self, key, value, expires_in_sec=None, **kwargs):
        CACHE[key] = value
        CACHE_TTL[key] = expires_in_sec

    def get_value(self, key, **kwargs):
        return CACHE.get(key)

    def delete_value(self, key, **kwargs):
        CACHE.pop(key, None)
        CACHE_TTL.pop(key, None)


# ── The fake ──────────────────────────────────────────────────────────────────

class _Meta:
    def __init__(self, doctype):
        self._fields = META_FIELDS.get(doctype, set())

    def has_field(self, fieldname):
        return fieldname in self._fields

    def get_field(self, fieldname):
        return {"fieldname": fieldname} if fieldname in self._fields else None


def _filter_matches(doc, filters):
    """Evaluate dict filters against a doc, including the ["op", value] form.

    Real frappe accepts `{"disabled": ["!=", 1]}` alongside plain equality; a
    stub that only did equality silently matched nothing for those, so code
    guarded by an operator filter looked broken in tests when it was fine.
    """
    for key, cond in (filters or {}).items():
        actual = doc.get(key)
        if isinstance(cond, (list, tuple)) and len(cond) == 2:
            op, expected = cond
            op = str(op).lower()
            if op == "!=":
                if actual == expected:
                    return False
            elif op == "=":
                if actual != expected:
                    return False
            elif op == "in":
                if actual not in expected:
                    return False
            elif op == "not in":
                if actual in expected:
                    return False
            elif op == "is":
                is_set = actual not in (None, "")
                if (expected == "set") != is_set:
                    return False
            else:
                raise NotImplementedError(f"frappe_stub: filter operator {op!r}")
        elif actual != cond:
            return False
    return True


def _db_get_value(doctype, filters, fieldname=None, as_dict=False, **kwargs):
    if isinstance(filters, str):
        if filters not in DB.get(doctype, {}):
            return None            # real frappe returns None for a missing doc
        doc = DB[doctype][filters]
        if isinstance(fieldname, (list, tuple)):
            picked = {f: doc.get(f) for f in fieldname}
            # Real frappe returns a frappe._dict (a dict subclass) when
            # as_dict=True, so callers use .get() on it. Returning a
            # SimpleNamespace here made the stub disagree with production.
            return dict(picked) if as_dict else types.SimpleNamespace(**picked)
        return doc.get(fieldname)
    # dict filters — first doc whose fields all match, honouring order_by so a
    # test can pin which of several matching docs production picks.
    matching = [
        (name, doc) for name, doc in DB.get(doctype, {}).items()
        if _filter_matches(doc, filters)
    ]
    order_by = kwargs.get("order_by")
    if order_by:
        field, _, direction = order_by.partition(" ")
        matching.sort(
            key=lambda pair: (pair[1].get(field) is None, pair[1].get(field)),
            reverse=direction.strip().lower() == "desc",
        )
    for name, doc in matching:
        return name if fieldname == "name" else doc.get(fieldname)
    return None


def _db_set_value(doctype, name, values, value=None, **kwargs):
    if isinstance(values, str):
        values = {values: value}
    WRITES.append((doctype, name, dict(values), dict(kwargs)))
    DB.setdefault(doctype, {}).setdefault(name, {}).update(values)


def _db_sql(query, params=None, as_dict=False, **kwargs):
    for needle, rows in SQL_RESULTS.items():
        if needle in " ".join(query.split()):
            return rows
    return []


class _NewDoc:
    """Stand-in for the doc returned by frappe.get_doc({...}).

    Supports the surface this app uses on a brand-new doc: attribute get/set,
    .get(), .append() for child tables, .insert() and .save().  Inserting
    records the dict in INSERTS and seeds DB, so a test can assert both what
    was created and — the point of most of these tests — that nothing was.
    """

    def __init__(self, values):
        self.__dict__["_values"] = dict(values)
        self.__dict__["flags"] = types.SimpleNamespace(ignore_permissions=False)
        doctype = values.get("doctype", "DOC")
        self.__dict__["_values"].setdefault(
            "name", values.get("name") or f"{doctype}-{len(INSERTS) + 1:04d}"
        )

    def __getattr__(self, key):
        try:
            return self.__dict__["_values"][key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self.__dict__["_values"][key] = value

    def get(self, key, default=None):
        return self._values.get(key, default)

    def append(self, fieldname, row):
        self._values.setdefault(fieldname, []).append(dict(row))
        return row

    def insert(self, *a, **k):
        INSERTS.append(dict(self._values))
        doctype = self._values.get("doctype", "DOC")
        DB.setdefault(doctype, {})[self._values["name"]] = dict(self._values)
        return self

    def save(self, *a, **k):
        doctype = self._values.get("doctype", "DOC")
        DB.setdefault(doctype, {}).setdefault(self._values["name"], {}).update(self._values)
        return self


def _get_doc(doctype, name=None, **k):
    # frappe.get_doc({...}) — building a new doc
    if isinstance(doctype, dict):
        return _NewDoc(doctype)
    # frappe.get_doc("Doctype", "name") — loading an existing one
    return types.SimpleNamespace(
        name=name,
        get=lambda key, default=None: get_doc_values(doctype, name).get(key, default),
    )


def install():
    """Install the fake into sys.modules and return it.  Idempotent."""
    if "frappe" in sys.modules and getattr(sys.modules["frappe"], "_is_shopify_stub", False):
        return sys.modules["frappe"]

    reset()

    frappe = types.ModuleType("frappe")
    frappe._is_shopify_stub = True
    # Makes `frappe` importable as a package so `from frappe.model.document
    # import Document` resolves through the submodules registered below.
    frappe.__path__ = []

    def _noop(*args, **kwargs):
        return None

    frappe.log_error = lambda message="", title="": ERRORS.append((str(message), str(title)))
    frappe.logger = lambda *a, **k: types.SimpleNamespace(
        info=_noop, warning=_noop, error=_noop, debug=_noop
    )
    frappe.get_meta      = lambda doctype: _Meta(doctype)
    frappe.get_all       = lambda *a, **k: []
    frappe.get_doc       = _get_doc
    frappe.whitelist     = lambda *a, **k: (lambda fn: fn)
    frappe.enqueue       = _noop
    frappe.get_traceback = lambda *a, **k: "traceback"

    # Real frappe.throw raises ValidationError. A no-op stub would let code that
    # is supposed to refuse bad input sail straight past the guard, and the test
    # would pass on behaviour that does not exist.
    class ValidationError(Exception):
        pass

    class PermissionError_(Exception):
        pass

    frappe.ValidationError = ValidationError
    frappe.PermissionError = PermissionError_

    def _throw(msg="", exc=None, title=None, **kwargs):
        raise (exc or ValidationError)(str(msg))

    frappe.throw = _throw
    frappe.has_permission = lambda *a, **k: True
    frappe.session = types.SimpleNamespace(user="Administrator")
    frappe.cache = lambda: _Cache()

    frappe.db = types.SimpleNamespace(
        get_value=_db_get_value,
        set_value=_db_set_value,
        sql=_db_sql,
        commit=lambda: COMMITS.append(1),
        exists=lambda *a, **k: True,
        has_column=lambda doctype, column: column in COLUMNS.get(doctype, set()),
    )

    utils = types.ModuleType("frappe.utils")
    utils.cint         = lambda v: int(v or 0)
    utils.flt          = lambda v, *a: float(v or 0)
    utils.now_datetime = lambda: datetime.datetime(2026, 8, 26, 12, 0, 0)
    utils.add_to_date  = _add_to_date
    utils.nowdate      = lambda: "2026-08-26"
    utils.getdate      = lambda v=None: v

    # frappe.model.document.Document — DocType controllers subclass it.
    model = types.ModuleType("frappe.model")
    model.__path__ = []
    document = types.ModuleType("frappe.model.document")

    class Document:
        """Minimal stand-in; controllers here only ever subclass it."""

        def get(self, key, default=None):
            return getattr(self, key, default)

    document.Document = Document
    model.document = document

    password = types.ModuleType("frappe.utils.password")
    password.get_decrypted_password = (
        lambda doctype, name, fieldname, **k: PASSWORDS.get((doctype, name, fieldname), "")
    )

    frappe.utils = utils
    frappe.model = model
    sys.modules["frappe"]                 = frappe
    sys.modules["frappe.utils"]           = utils
    sys.modules["frappe.utils.password"]  = password
    sys.modules["frappe.model"]           = model
    sys.modules["frappe.model.document"]  = document
    return frappe


class FakeSettings:
    """Stand-in for a Shopify Settings document."""

    def __init__(self, name="Test Store", shop_domain="notdrones.myshopify.com",
                 token="shpat_x", client_id="", client_secret=""):
        self.name = name
        self._values = {
            "name": name,
            "shop_domain": shop_domain,
            "api_version": "2026-01",
            # Client ID is a plain Data field, so it lives on the doc...
            "admin_api_client_id": client_id,
        }
        # ...while the token and the secret are Password fields, which production
        # code reads via get_decrypted_password rather than off the doc. Register
        # them where the fake serves that call from.
        PASSWORDS[("Shopify Settings", name, "admin_api_access_token")] = token
        PASSWORDS[("Shopify Settings", name, "admin_api_client_secret")] = client_secret

    def get(self, key, default=None):
        return self._values.get(key, default)
