from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr, conint, confloat
from typing import List, Optional, Dict, Any
import smtplib
import ssl
import time
import random
from email.message import EmailMessage
from email.utils import make_msgid
import threading
import uuid

app = FastAPI()

# ---------- Pydantic models (API schema) ----------

class Sender(BaseModel):
    email: EmailStr
    password: str
    limit: conint(gt=0)  # max prospects for this sender


class FollowUp(BaseModel):
    delay_minutes: confloat(gt=0)  # after initial email
    body: str


class CampaignRequest(BaseModel):
    smtp_server: str
    smtp_port: int

    campaign_name: Optional[str] = None
    subject: str
    body: str

    prospects: List[EmailStr]

    min_delay_minutes: confloat(gt=0)
    max_delay_minutes: confloat(gt=0)

    senders: List[Sender]
    followups: List[FollowUp] = []


class CampaignStatus(BaseModel):
    id: str
    campaign_name: Optional[str]
    total_prospects: int
    total_senders: int
    finished: bool
    log: List[str]


# ---------- In-memory store (demo only – for production use DB/Redis) ----------

campaigns: Dict[str, Dict[str, Any]] = {}
campaigns_lock = threading.Lock()


def log_event(campaign_id: str, message: str) -> None:
    """Append a log line to this campaign (thread-safe)."""
    with campaigns_lock:
        data = campaigns.get(campaign_id)
        if not data:
            return
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        data["log"].append(f"[{timestamp}] {message}")


# ---------- Core campaign logic (adapted from your Tkinter version) ----------

def run_campaign(campaign_id: str, req: CampaignRequest) -> None:
    """Runs in a background thread; can use sleep here without blocking any API request."""
    context = ssl.create_default_context()

    try:
        # basic validation
        if req.max_delay_minutes < req.min_delay_minutes:
            log_event(campaign_id, "Error: max_delay_minutes must be >= min_delay_minutes")
            return

        prospects = list(req.prospects)
        num_prospects = len(prospects)
        senders_data = req.senders

        # capacity check
        total_capacity = sum(s.limit for s in senders_data)
        if total_capacity < num_prospects:
            log_event(
                campaign_id,
                f"Error: total sender capacity ({total_capacity}) < prospects ({num_prospects})"
            )
            return

        num_senders = len(senders_data)

        # === assignment logic: shuffled rounds ===
        remaining = [s.limit for s in senders_data]
        sender_slots = []

        while len(sender_slots) < num_prospects:
            available = [i for i in range(num_senders) if remaining[i] > 0]
            if not available:
                break
            random.shuffle(available)
            for s_idx in available:
                if remaining[s_idx] <= 0:
                    continue
                sender_slots.append(s_idx)
                remaining[s_idx] -= 1
                if len(sender_slots) >= num_prospects:
                    break

        prospect_sender_indices = sender_slots[:num_prospects]

        log_event(campaign_id, f"Starting campaign '{req.campaign_name or '(no name)'}'")
        log_event(campaign_id, f"Total prospects: {num_prospects}")
        log_event(campaign_id, f"Number of senders: {num_senders}")
        log_event(
            campaign_id,
            f"Random delay between prospects: {req.min_delay_minutes}–{req.max_delay_minutes} minutes"
        )
        log_event(campaign_id, f"Follow-ups per prospect: {len(req.followups)}")

        # === schedule initial send times ===
        now = time.time()
        inter_delays = [
            random.uniform(req.min_delay_minutes, req.max_delay_minutes) * 60.0
            for _ in range(num_prospects - 1)
        ]

        initial_times = []
        t = now
        for i in range(num_prospects):
            if i == 0:
                t = now
            else:
                t += inter_delays[i - 1]
            initial_times.append(t)

        # === build tasks (initial + follow-ups) ===
        tasks = []
        thread_root_ids = [make_msgid() for _ in range(num_prospects)]

        for i, prospect in enumerate(prospects):
            sender_index = prospect_sender_indices[i]
            root_id = thread_root_ids[i]

            # initial email
            tasks.append({
                "time": initial_times[i],
                "prospect_index": i,
                "sender_index": sender_index,
                "type": "initial",
                "body": req.body,
                "subject": req.subject,
                "thread_root_id": root_id,
            })

            # follow-ups
            for f_idx, fu in enumerate(req.followups):
                scheduled_time = initial_times[i] + fu.delay_minutes * 60.0
                tasks.append({
                    "time": scheduled_time,
                    "prospect_index": i,
                    "sender_index": sender_index,
                    "type": "followup",
                    "body": fu.body,
                    "subject": f"Re: {req.subject}",
                    "thread_root_id": root_id,
                    "follow_index": f_idx,
                })

        tasks.sort(key=lambda x: x["time"])

        total_followups_per_prospect = [len(req.followups)] * num_prospects
        sent_followups_per_prospect = [0] * num_prospects

        # === process tasks over time ===
        for task in tasks:
            wait_seconds = task["time"] - time.time()
            if wait_seconds > 0:
                time.sleep(wait_seconds)

            p_index = task["prospect_index"]
            sender_index = task["sender_index"]
            to_addr = prospects[p_index]
            sender = senders_data[sender_index]
            sender_email = sender.email
            sender_password = sender.password

            msg = EmailMessage()
            msg["From"] = sender_email
            msg["To"] = to_addr
            msg["Subject"] = task["subject"]
            msg.set_content(task["body"])

            if task["type"] == "initial":
                msg["Message-ID"] = task["thread_root_id"]
            else:
                follow_msg_id = make_msgid()
                msg["Message-ID"] = follow_msg_id
                msg["In-Reply-To"] = task["thread_root_id"]
                msg["References"] = task["thread_root_id"]

            try:
                with smtplib.SMTP(req.smtp_server, req.smtp_port) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(sender_email, sender_password)
                    server.send_message(msg)

                if task["type"] == "initial":
                    log_event(
                        campaign_id,
                        f"Initial email sent from {sender_email} to {to_addr}"
                    )
                else:
                    fu_num = task.get("follow_index", 0) + 1
                    sent_followups_per_prospect[p_index] += 1
                    log_event(
                        campaign_id,
                        f"Follow-up #{fu_num} sent from {sender_email} to {to_addr}"
                    )
            except Exception as e:
                if task["type"] == "initial":
                    log_event(
                        campaign_id,
                        f"ERROR sending initial email from {sender_email} to {to_addr}: {e}"
                    )
                else:
                    fu_num = task.get("follow_index", 0) + 1
                    log_event(
                        campaign_id,
                        f"ERROR sending follow-up #{fu_num} from {sender_email} to {to_addr}: {e}"
                    )
                continue

        log_event(campaign_id, "Campaign finished. All scheduled emails and follow-ups processed.")

    except Exception as e:
        log_event(campaign_id, f"Fatal error: {e}")
    finally:
        with campaigns_lock:
            if campaign_id in campaigns:
                campaigns[campaign_id]["finished"] = True


# ---------- API endpoints ----------

@app.post("/campaign", response_model=CampaignStatus)
def start_campaign(req: CampaignRequest):
    """Start a campaign. Returns a campaign ID immediately; sending happens in background."""
    if not req.senders:
        raise HTTPException(status_code=400, detail="At least one sender is required")
    if not req.prospects:
        raise HTTPException(status_code=400, detail="At least one prospect is required")
    if req.max_delay_minutes < req.min_delay_minutes:
        raise HTTPException(status_code=400, detail="max_delay_minutes must be >= min_delay_minutes")

    campaign_id = str(uuid.uuid4())

    with campaigns_lock:
        campaigns[campaign_id] = {
            "request": req,
            "log": [],
            "finished": False,
        }

    # background thread so the HTTP call returns instantly
    thread = threading.Thread(target=run_campaign, args=(campaign_id, req), daemon=True)
    thread.start()

    return CampaignStatus(
        id=campaign_id,
        campaign_name=req.campaign_name,
        total_prospects=len(req.prospects),
        total_senders=len(req.senders),
        finished=False,
        log=[],
    )


@app.get("/campaign/{campaign_id}", response_model=CampaignStatus)
def get_campaign_status(campaign_id: str):
    """Poll campaign state + logs."""
    with campaigns_lock:
        data = campaigns.get(campaign_id)
        if not data:
            raise HTTPException(status_code=404, detail="Campaign not found")

        req: CampaignRequest = data["request"]

        return CampaignStatus(
            id=campaign_id,
            campaign_name=req.campaign_name,
            total_prospects=len(req.prospects),
            total_senders=len(req.senders),
            finished=data["finished"],
            log=data["log"],
        )
