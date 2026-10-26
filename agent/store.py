"""In-memory stand-in for the hospital's systems of record.

Each collection maps to a real integration named in the product brief:
  patients / appointments / slots / waitlist  -> HIS appointment module (write path)
  documents                                   -> document store + EMR
  policies / preauths / claims                -> billing, TPA portal, NHCX
  consents                                    -> purpose-scoped consent ledger (DPDP)
  outbox                                      -> WhatsApp Business / SMS / IVR gateway
  tasks                                       -> human work queues (front desk, insurance desk, clinician)
  audit / events                              -> audit log (retained >= 1 year) and metrics

Swap the methods for real adapters; the tools layer only talks to this interface.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

CARE = "care_communication"  # reminders, follow-ups, care-plan nudges: part of care already consented to
MARKETING = "marketing_recall"  # recall/health-check campaigns: promotional, separate opt-in, DND applies
ABDM_SHARING = "abdm_record_sharing"  # ABHA linking / health-record exchange via the consent manager
INSURANCE_SHARING = "insurance_data_sharing"  # sending records to insurer/TPA/NHCX for pre-auth and claims
PURPOSES = (CARE, MARKETING, ABDM_SHARING, INSURANCE_SHARING)


@dataclass
class Patient:
    id: str
    name: str
    dob: str
    phone: str
    language: str = "en"  # en | hi | ta | ... ; the agent replies in this language
    preferred_channel: str = "whatsapp"  # whatsapp | sms | voice | web | kiosk
    abha: str | None = None  # optional; never a precondition for care
    guardian_name: str | None = None  # set for minors
    dnd: bool = False  # TRAI DND registry for SMS/voice promotional traffic
    allergies: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    medications: list[str] = field(default_factory=list)
    insurance_policy: str | None = None
    last_visit: date | None = None
    care_plan: list[dict] = field(default_factory=list)  # {"kind","what","due"} from the EMR


@dataclass
class Provider:
    id: str
    name: str
    specialty: str
    location: str


@dataclass
class Slot:
    id: str
    provider_id: str
    start: datetime
    minutes: int = 30
    booked: bool = False
    held_for: str | None = None  # patient offered this slot from the waitlist
    held_until: datetime | None = None


@dataclass
class Appointment:
    id: str
    patient_id: str
    provider_id: str
    slot_id: str
    start: datetime
    reason: str
    visit_type: str  # consult | follow_up | procedure | telehealth
    status: str = "booked"  # booked | confirmed | cancelled | completed | no_show
    reminders_sent: list[str] = field(default_factory=list)
    followup_done: bool = False
    source: str = "agent"  # agent | front_desk | waitlist_refill


@dataclass
class WaitlistEntry:
    id: str
    patient_id: str
    provider_id: str
    earliest: date
    latest: date
    reason: str
    offered_slot: str | None = None


@dataclass
class Document:
    id: str
    patient_id: str
    kind: str  # prescription | discharge_summary | lab_report | mri_report | id_proof | policy_card
    ocr_text: str
    extracted: dict = field(default_factory=dict)
    confirmed_fields: dict = field(default_factory=dict)


@dataclass
class Policy:
    number: str
    insurer: str
    tpa: str
    holder_name: str
    start: date
    valid_until: date
    sum_insured: int
    balance: int
    cashless: bool
    hospital_empanelled: bool
    copay_pct: int
    room_rent_limit: int  # per day
    waiting_periods_months: dict[str, int] = field(default_factory=dict)
    sub_limits: dict[str, int] = field(default_factory=dict)
    exclusions: list[str] = field(default_factory=list)
    nhcx_enabled: bool = False


@dataclass
class PreAuth:
    id: str
    patient_id: str
    policy_number: str
    procedure: str
    estimated_cost: int
    stage: str  # initial | enhancement | final_discharge
    status: str  # pack_incomplete | queued_for_desk | submitted | query_raised | approved | partially_approved | rejected
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    approved_amount: int = 0
    insurer_note: str = ""
    insurer_reference: str | None = None  # present only once the insurer/TPA has confirmed in writing
    documents: list[str] = field(default_factory=list)


@dataclass
class Claim:
    id: str
    patient_id: str
    policy_number: str
    preauth_id: str | None
    amount: int
    procedure_code: str
    status: str  # draft | qa_failed | submitted | under_review | settled | rejected
    attached: list[str] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)
    first_pass: bool | None = None
    rejection_reasons: list[str] = field(default_factory=list)


@dataclass
class PendingAction:
    """A state change the model has proposed but that has not been committed.

    It commits only after (a) the read-back was shown to the patient and (b) the patient's next
    message is an explicit yes. The idempotency key makes retries safe against the HIS.
    """

    token: str
    kind: str
    patient_id: str
    payload: dict
    readback: str
    idempotency_key: str
    proposed_at_turn: int
    status: str = "pending"  # pending | committed | superseded


class Store:
    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2026, 10, 26, 9, 0)
        self._ids = itertools.count(1000)
        self.patients: dict[str, Patient] = {}
        self.providers: dict[str, Provider] = {}
        self.slots: dict[str, Slot] = {}
        self.appointments: dict[str, Appointment] = {}
        self.waitlist: dict[str, WaitlistEntry] = {}
        self.documents: dict[str, Document] = {}
        self.policies: dict[str, Policy] = {}
        self.preauths: dict[str, PreAuth] = {}
        self.claims: dict[str, Claim] = {}
        self.pending: dict[str, PendingAction] = {}
        self.committed: dict[str, dict] = {}  # idempotency_key -> result
        self.consents: list[dict] = []  # append-only ledger
        self.suppression: set[str] = set()  # phone numbers that replied STOP to promotional traffic
        self.outbox: list[dict] = []
        self.tasks: list[dict] = []
        self.audit: list[dict] = []
        self.events: list[dict] = []
        self.content: dict[str, dict] = {}
        self.campaign_sent: set[str] = set()  # dedupe keys for proactive messages
        self._seed()

    # ---- infrastructure -------------------------------------------------------------------------

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)

    def log(self, action: str, **details) -> None:
        self.audit.append({"at": self.now.isoformat(timespec="minutes"), "action": action, **details})

    def event(self, name: str, **details) -> None:
        self.events.append({"at": self.now, "event": name, **details})

    def add_task(self, queue: str, priority: str, summary: str, **details) -> dict:
        task = {
            "id": self.new_id("TASK"), "queue": queue, "priority": priority, "summary": summary,
            "created": self.now.isoformat(timespec="minutes"), "status": "open", **details,
        }
        self.tasks.append(task)
        self.log("task_created", task=task["id"], queue=queue, priority=priority)
        return task

    def send(self, patient_id: str, text: str, purpose: str, template: str | None = None) -> dict:
        p = self.patients[patient_id]
        msg = {
            "id": self.new_id("MSG"), "to": p.phone, "patient_id": patient_id, "channel": p.preferred_channel,
            "purpose": purpose, "template": template, "text": text, "at": self.now.isoformat(timespec="minutes"),
        }
        self.outbox.append(msg)
        self.log("message_sent", patient=patient_id, purpose=purpose, template=template)
        return msg

    # ---- consent ledger ---------------------------------------------------------------------------

    def set_consent(self, patient_id: str, purpose: str, granted: bool, channel: str, source: str) -> None:
        self.consents.append({
            "patient_id": patient_id, "purpose": purpose, "granted": granted, "channel": channel,
            "source": source, "at": self.now.isoformat(timespec="minutes"),
        })
        if purpose == MARKETING and not granted:
            self.suppression.add(self.patients[patient_id].phone)
        self.log("consent_" + ("granted" if granted else "withdrawn"), patient=patient_id, purpose=purpose)

    def has_consent(self, patient_id: str, purpose: str) -> bool:
        for entry in reversed(self.consents):
            if entry["patient_id"] == patient_id and entry["purpose"] == purpose:
                return entry["granted"]
        return False

    # ---- seed data --------------------------------------------------------------------------------

    def _seed(self) -> None:
        for p in [
            Provider("DR-1", "Dr. Ananya Rao", "general_medicine", "Main Block, OPD 4"),
            Provider("DR-2", "Dr. Vikram Shah", "orthopedics", "Main Block, OPD 12"),
            Provider("DR-3", "Dr. Meera Iyer", "cardiology", "Heart Centre, Block B"),
            Provider("DR-4", "Dr. Sam Okafor", "pediatrics", "Mother & Child Wing"),
            Provider("DR-5", "Dr. Kavita Menon", "endocrinology", "Main Block, OPD 7"),
        ]:
            self.providers[p.id] = p

        day0 = self.now.replace(hour=0, minute=0)
        for d in range(0, 14):
            day = day0 + timedelta(days=d)
            if day.weekday() == 6:  # OPD closed Sundays
                continue
            for prov in self.providers:
                for hour, minute in [(9, 0), (9, 30), (10, 0), (11, 0), (12, 0), (15, 0), (16, 0), (17, 0)]:
                    start = day.replace(hour=hour, minute=minute)
                    if start <= self.now:
                        continue
                    sid = self.new_id("SLOT")
                    self.slots[sid] = Slot(sid, prov, start)

        self.policies["STAR-778812"] = Policy(
            "STAR-778812", "Star Health", "MediAssist TPA", "Ravi Kumar", date(2023, 4, 1), date(2027, 3, 31),
            sum_insured=500000, balance=420000, cashless=True, hospital_empanelled=True, copay_pct=10,
            room_rent_limit=5000, waiting_periods_months={"knee replacement": 24, "cataract": 24},
            sub_limits={"cataract": 40000, "knee arthroscopy": 150000},
            exclusions=["cosmetic surgery", "dental implants"], nhcx_enabled=True,
        )
        self.policies["HDFC-551200"] = Policy(
            "HDFC-551200", "HDFC ERGO", "Paramount TPA", "Priya Nair", date(2025, 10, 1), date(2026, 9, 30),
            sum_insured=300000, balance=300000, cashless=True, hospital_empanelled=True, copay_pct=0,
            room_rent_limit=4000, waiting_periods_months={"hernia repair": 24},
        )

        self.patients["PT-1"] = Patient(
            "PT-1", "Ravi Kumar", "1979-04-12", "+919845011111", language="hi",
            conditions=["type 2 diabetes", "hypertension"], medications=["metformin 500mg", "amlodipine 5mg"],
            insurance_policy="STAR-778812", last_visit=date(2026, 7, 2), abha="91-4021-7788-1234",
            care_plan=[
                {"kind": "lab_due", "what": "HbA1c", "due": "2026-10-28"},
                {"kind": "medication_refill", "what": "metformin 500mg", "due": "2026-10-30"},
            ],
        )
        self.patients["PT-2"] = Patient(
            "PT-2", "Priya Nair", "1991-11-03", "+919845022222", preferred_channel="sms",
            insurance_policy="HDFC-551200", last_visit=date(2025, 9, 15), dnd=True,
        )
        self.patients["PT-3"] = Patient(
            "PT-3", "Arjun Mehta", "2018-06-20", "+919845033333", guardian_name="Neha Mehta",
            last_visit=date(2026, 10, 24),
        )
        self.patients["PT-4"] = Patient(
            "PT-4", "Lakshmi Subramanian", "1958-02-17", "+919845044444", language="ta", preferred_channel="voice",
            conditions=["type 2 diabetes"], last_visit=date(2025, 8, 10),
        )
        for pid in self.patients:
            self.set_consent(pid, CARE, True, "registration_desk", "paper form, digitised")
        self.set_consent("PT-1", INSURANCE_SHARING, True, "whatsapp", "agent")
        self.set_consent("PT-4", MARKETING, True, "registration_desk", "paper form, digitised")
        self.set_consent("PT-2", MARKETING, True, "web", "website signup")
        self.audit.clear()

        # Ravi: ortho pre-op consult booked in 2 days; pre-auth has a TPA query; an older claim lacks documents.
        slot = next(s for s in self.slots.values() if s.provider_id == "DR-2" and s.start.date() == date(2026, 10, 28))
        slot.booked = True
        self.appointments["APT-1"] = Appointment(
            "APT-1", "PT-1", "DR-2", slot.id, slot.start, "pre-op consult, left knee arthroscopy", "procedure",
            source="front_desk",
        )
        self.preauths["PA-1"] = PreAuth(
            "PA-1", "PT-1", "STAR-778812", "knee arthroscopy", 180000, "initial", "query_raised",
            requested_at=self.now - timedelta(hours=20), insurer_reference="MA-Q-55120",
            insurer_note="TPA requests MRI report of left knee and orthopaedician's treatment notes.",
            documents=["policy_card", "id_proof", "consultation_note"],
        )
        self.claims["CLM-1"] = Claim(
            "CLM-1", "PT-1", "STAR-778812", None, 12500, "99213", "draft",
            attached=["final_bill", "prescription"], signatures=["patient"],
        )
        self.documents["DOC-1"] = Document(
            "DOC-1", "PT-1", "prescription",
            "Dr. P. Gupta Clinic 03/07/2026 | Pt: Ravi Kumar 47M | Dx: T2DM, HTN | "
            "Rx: Tab Metformin 500 mg 1-0-1 x 90d; Tab Amlodipine 5 mg 0-0-1 x 90d | "
            "Allergy: Sulfa drugs | Review after 3 months with HbA1c",
        )
        # Arjun: paediatric consult two days ago, no follow-up check-in yet.
        self.appointments["APT-0"] = Appointment(
            "APT-0", "PT-3", "DR-4", "past", datetime(2026, 10, 24, 10, 0), "fever and cough", "consult",
            status="completed",
        )
        # Priya: waitlisted for an earlier endocrinology slot.
        self.waitlist["WL-1"] = WaitlistEntry("WL-1", "PT-2", "DR-5", date(2026, 10, 27), date(2026, 11, 3), "thyroid review")

        # Clinician-approved content. The agent may only state clinical/prep facts that come from here.
        self.content = {
            "prep:knee arthroscopy": {
                "approved_by": "Dr. Vikram Shah", "version": "2026-08",
                "text": "Bring MRI films and reports, current medicine list and policy card. Wear loose clothing. "
                        "If you take blood thinners, tell the doctor at the consult - do not stop any medicine on your own.",
            },
            "prep:fasting blood test": {
                "approved_by": "Lab Director", "version": "2026-01",
                "text": "No food for 10-12 hours before the test. Water is allowed. Take regular medicines unless your doctor said otherwise.",
            },
            "followup:pediatric fever": {
                "approved_by": "Dr. Sam Okafor", "version": "2026-05",
                "questions": [
                    "Is the fever lower than at the visit?",
                    "Is your child drinking fluids and passing urine normally?",
                    "Any fast breathing, drowsiness, rash or fits?",
                    "Are you able to give the medicines as prescribed?",
                ],
                "red_flag_answers": ["fast breathing", "drowsy", "rash", "fits", "seizure", "not drinking", "no urine"],
            },
            "followup:post-procedure": {
                "approved_by": "Nursing Head", "version": "2026-04",
                "questions": [
                    "How is your pain on a scale of 0-10?",
                    "Any fever, bleeding or discharge from the wound?",
                    "Are you able to take your medicines as prescribed?",
                    "Do you have your review date?",
                ],
                "red_flag_answers": ["bleeding", "fever", "discharge", "swelling", "pain 8", "pain 9", "pain 10"],
            },
            "service:health check": {
                "approved_by": "Marketing + Medical Director", "version": "2026-09",
                "text": "Diabetes Annual Review package: HbA1c, lipid profile, kidney function, eye and foot screening, "
                        "endocrinologist consult. Rs 2,499.",
            },
        }
