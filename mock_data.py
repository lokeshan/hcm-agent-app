"""Mock Oracle HCM directory (9 sample employees) + query functions.
Used when data_source = mock. Field names mirror Oracle HCM `workers`.
"""
from __future__ import annotations

WORKERS = [
    {"PersonId":"300000012345678","PersonNumber":"100001","DisplayName":"Jane Doe","WorkEmail":"jane.doe@example.com","WorkPhoneNumber":"+91 80 4123 0001","JobTitle":"Senior Software Engineer","DepartmentName":"Engineering","LocationName":"Bangalore, IN","ManagerName":"Michael Chen","ManagerPersonId":"300000012345679","HireDate":"2021-03-15","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345679","PersonNumber":"100002","DisplayName":"Michael Chen","WorkEmail":"michael.chen@example.com","WorkPhoneNumber":"+91 80 4123 0002","JobTitle":"Engineering Manager","DepartmentName":"Engineering","LocationName":"Bangalore, IN","ManagerName":"Priya Nair","ManagerPersonId":"300000012345680","HireDate":"2018-07-01","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345680","PersonNumber":"100003","DisplayName":"Priya Nair","WorkEmail":"priya.nair@example.com","WorkPhoneNumber":"+91 80 4123 0003","JobTitle":"VP, Engineering","DepartmentName":"Engineering","LocationName":"Bangalore, IN","ManagerName":"Robert King","ManagerPersonId":"300000012345681","HireDate":"2015-02-10","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345681","PersonNumber":"100004","DisplayName":"Robert King","WorkEmail":"robert.king@example.com","WorkPhoneNumber":"+1 212 555 0104","JobTitle":"Chief Executive Officer","DepartmentName":"Executive","LocationName":"New York, US","ManagerName":None,"ManagerPersonId":None,"HireDate":"2012-01-05","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345682","PersonNumber":"100005","DisplayName":"Sara Williams","WorkEmail":"sara.williams@example.com","WorkPhoneNumber":"+44 20 7946 0105","JobTitle":"HR Business Partner","DepartmentName":"Human Resources","LocationName":"London, UK","ManagerName":"Linda Gomez","ManagerPersonId":"300000012345683","HireDate":"2020-09-21","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345683","PersonNumber":"100006","DisplayName":"Linda Gomez","WorkEmail":"linda.gomez@example.com","WorkPhoneNumber":"+44 20 7946 0106","JobTitle":"HR Director","DepartmentName":"Human Resources","LocationName":"London, UK","ManagerName":"Robert King","ManagerPersonId":"300000012345681","HireDate":"2016-11-30","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345684","PersonNumber":"100007","DisplayName":"David Smith","WorkEmail":"david.smith@example.com","WorkPhoneNumber":"+1 212 555 0107","JobTitle":"Financial Analyst","DepartmentName":"Finance","LocationName":"New York, US","ManagerName":"Emily Turner","ManagerPersonId":"300000012345685","HireDate":"2022-06-13","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345685","PersonNumber":"100008","DisplayName":"Emily Turner","WorkEmail":"emily.turner@example.com","WorkPhoneNumber":"+1 212 555 0108","JobTitle":"Finance Manager","DepartmentName":"Finance","LocationName":"New York, US","ManagerName":"Robert King","ManagerPersonId":"300000012345681","HireDate":"2017-04-18","AssignmentStatus":"ACTIVE"},
    {"PersonId":"300000012345686","PersonNumber":"100009","DisplayName":"Ahmed Khan","WorkEmail":"ahmed.khan@example.com","WorkPhoneNumber":"+91 80 4123 0009","JobTitle":"Product Manager","DepartmentName":"Product","LocationName":"Bangalore, IN","ManagerName":"Priya Nair","ManagerPersonId":"300000012345680","HireDate":"2019-08-05","AssignmentStatus":"ACTIVE"},
]
_BY_ID = {w["PersonId"]: w for w in WORKERS}
_BY_NUM = {w["PersonNumber"]: w for w in WORKERS}
_LIGHT = lambda w: {k: w[k] for k in ("PersonId","PersonNumber","DisplayName","JobTitle","DepartmentName","WorkEmail")}


def search_workers(query):
    q = (query or "").lower().strip()
    if not q:
        return []
    fields = ("DisplayName","WorkEmail","DepartmentName","JobTitle","PersonNumber")
    return [_LIGHT(w) for w in WORKERS if any(q in str(w.get(f,"")).lower() for f in fields)]


def get_worker(person_id):
    w = _BY_ID.get(str(person_id)) or _BY_NUM.get(str(person_id))
    if not w:
        return {"error": "not_found"}
    return {k: v for k, v in w.items() if k != "ManagerPersonId"}


def get_assignment(person_id):
    w = _BY_ID.get(str(person_id)) or _BY_NUM.get(str(person_id))
    if not w:
        return {"error": "not_found"}
    return {k: w[k] for k in ("PersonId","JobTitle","DepartmentName","LocationName","ManagerName","ManagerPersonId","HireDate","AssignmentStatus")}


def get_direct_reports(person_id):
    return [_LIGHT(w) for w in WORKERS if w.get("ManagerPersonId") == str(person_id)]


def list_by_department(department):
    q = (department or "").lower().strip()
    if not q:
        return []
    return [_LIGHT(w) for w in WORKERS if q in str(w.get("DepartmentName","")).lower() or str(w.get("DepartmentName","")).lower() in q]


def team_goals(person_id):
    """Offline stand-in for the manager fan-out."""
    out = []
    for r in get_direct_reports(person_id):
        pn = r.get("PersonNumber")
        out.append({"PersonNumber": pn, "DisplayName": r.get("DisplayName"), "GoalCount": 2,
                    "Goals": [{"Goal": "Deliver Q3 roadmap", "Progress": "60",
                               "Target": "2026-09-30", "Status": "In progress"},
                              {"Goal": "Mentor a junior engineer", "Progress": "100",
                               "Target": "2026-06-30", "Status": "Completed"}]})
    return out


def run_custom(cfg, args):
    """Mock executor for CONFIG tools so UI-added tools are demonstrably callable offline."""
    kind = cfg.get("kind"); name = cfg.get("name", "tool")
    if kind == "oracle_search":
        return search_workers(args.get("query", ""))
    pid = (args.get(cfg.get("arg", "person_id")) or args.get("person_id")
           or next(iter(args.values()), None))
    w = _BY_ID.get(str(pid)) or _BY_NUM.get(str(pid))
    base = {"PersonId": pid, "DisplayName": (w or {}).get("DisplayName")}
    if kind == "oracle_child":
        return [dict(base, sample=f"{name} row 1"), dict(base, sample=f"{name} row 2")]
    if kind == "oracle_detail":
        return dict(base, detail=f"{name} sample value")
    if kind == "oracle_resource":
        res = cfg.get("endpoint") or ""
        if "earning" in res:
            return [{"Course": "Security Awareness 2026", "Type": "Course", "Status": "Completed",
                     "Due": "2026-03-31", "Completed": "2026-03-02", "Score": "92"},
                    {"Course": "Leading Hybrid Teams", "Type": "Specialization",
                     "Status": "In progress", "Due": "2026-09-30", "Completed": None, "Score": None}]
        return [{"Goal": "Deliver Q3 roadmap", "Progress": "60", "Target": "2026-09-30",
                 "Status": "In progress", "Weight": "40", "Plan": "FY26 Goal Plan"},
                {"Goal": "Mentor a junior engineer", "Progress": "100", "Target": "2026-06-30",
                 "Status": "Completed", "Weight": "20", "Plan": "FY26 Goal Plan"}]
    if kind == "external":
        # offline stand-in so an external tool is demonstrably callable without the
        # third-party system; echoes the call rather than pretending it succeeded.
        return {"mocked": True, "tool": name, "args": args,
                "would_call": f"{cfg.get('method', 'GET')} /{str(cfg.get('endpoint') or '').lstrip('/')}"}
    return {"error": "unsupported_kind", "kind": kind}


def overview():
    """Headcount by department + spans of control (for the HR Reports page)."""
    deps = {}
    for w in WORKERS:
        deps[w["DepartmentName"]] = deps.get(w["DepartmentName"], 0) + 1
    spans = []
    for w in WORKERS:
        n = len(get_direct_reports(w["PersonId"]))
        if n:
            spans.append({"manager": w["DisplayName"], "title": w["JobTitle"], "reports": n})
    spans.sort(key=lambda x: -x["reports"])
    by = [{"department": k, "count": v} for k, v in sorted(deps.items(), key=lambda x: -x[1])]
    return {"total": len(WORKERS), "by_department": by, "spans": spans}


def role_signals(person_id):
    """Simulate what Oracle would reveal to this person, so all three roles are
    demoable without live data: reports => manager; HR dept => sees beyond reports.
    (Live mode replaces this with a real per-user probe in hcm_client.)"""
    w = _BY_ID.get(str(person_id)) or _BY_NUM.get(str(person_id))
    if not w:
        return {"report_count": 0, "others_seen": 0, "secured_beyond_reports": False}
    reports = get_direct_reports(w["PersonId"])
    rc = len(reports)
    hr_hint = str(w.get("DepartmentName", "")).strip().lower() == "human resources"
    return {"report_count": rc, "others_seen": len(WORKERS) - 1 - rc,
            "secured_beyond_reports": hr_hint, "hr_hint": hr_hint}


def get_management_chain(person_id):
    chain, seen = [], set()
    w = _BY_ID.get(str(person_id)) or _BY_NUM.get(str(person_id))
    while w and w.get("ManagerPersonId") and w["ManagerPersonId"] not in seen:
        seen.add(w["ManagerPersonId"])
        m = _BY_ID.get(w["ManagerPersonId"])
        if not m:
            break
        chain.append({"PersonId": m["PersonId"], "DisplayName": m["DisplayName"], "JobTitle": m["JobTitle"]})
        w = m
    return chain
