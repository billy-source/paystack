"""
Paystack M-Pesa STK Push (Kenya)
================================
Sends an STK push prompt to a customer's phone, then checks the payment status.

Setup:
    pip install requests flask
    export PAYSTACK_SECRET_KEY="sk_test_xxxxxxxx"      # Mac/Linux
    setx PAYSTACK_SECRET_KEY "sk_test_xxxxxxxx"        # Windows (restart terminal)

Usage:
    python paystack_stk.py charge 0712345678 100 customer@email.com
    python paystack_stk.py verify <reference>
    python paystack_stk.py webhook            # optional: receive payment notifications

Notes:
    - Use your TEST key (sk_test_...) first. Switch to sk_live_... when ready.
    - Money lands in your Paystack balance, then settles to the bank/M-Pesa
      account you set up in your Paystack Dashboard (Settings -> Transfers/Settlements).
    - Never put your secret key in front-end code or commit it to GitHub.
"""

import hashlib
import hmac
import os
import sys
import time
import uuid

import requests

BASE_URL = "https://api.paystack.co"
SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")


def _headers() -> dict:
    if not SECRET_KEY:
        sys.exit("Error: set the PAYSTACK_SECRET_KEY environment variable first.")
    return {
        "Authorization": f"Bearer {SECRET_KEY}",
        "Content-Type": "application/json",
    }


def format_phone(phone: str) -> str:
    """Convert 07XXXXXXXX / 01XXXXXXXX / 254XXXXXXXXX to +254XXXXXXXXX."""
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+254"):
        return phone
    if phone.startswith("254"):
        return "+" + phone
    if phone.startswith("0"):
        return "+254" + phone[1:]
    if phone.startswith(("7", "1")) and len(phone) == 9:
        return "+254" + phone
    raise ValueError(f"Invalid Kenyan phone number: {phone}")


def stk_push(phone: str, amount_kes: float, email: str, reference: str | None = None) -> dict:
    """Send the M-Pesa prompt to the customer's phone."""
    reference = reference or f"stk-{uuid.uuid4().hex[:12]}"

    payload = {
        "email": email,
        "amount": int(round(amount_kes * 100)),  # Paystack uses the smallest unit (KES 100 -> 10000)
        "currency": "KES",
        "reference": reference,
        "mobile_money": {
            "phone": format_phone(phone),
            "provider": "mpesa",
        },
    }

    resp = requests.post(f"{BASE_URL}/charge", json=payload, headers=_headers(), timeout=30)
    data = resp.json()

    inner = data.get("data") or {}
    print(f"[debug] HTTP {resp.status_code} | message: {data.get('message')} | charge status: {inner.get('status')}")

    # "Charge attempted" is Paystack's normal reply when it accepts the charge and
    # sends the prompt, so it is NOT an error. Only fail on a real failure.
    failed = (
        not resp.ok
        or inner.get("status") == "failed"
        or (not data.get("status") and not inner)
    )
    if failed:
        reason = inner.get("gateway_response") or data.get("message") or resp.text
        raise RuntimeError(f"Paystack error: {reason}\nFull response: {data}")

    data["reference"] = reference
    return data


def verify(reference: str) -> dict:
    """Check the final status of a transaction."""
    resp = requests.get(f"{BASE_URL}/transaction/verify/{reference}", headers=_headers(), timeout=30)
    data = resp.json()
    if not resp.ok:
        raise RuntimeError(f"Paystack error: {data.get('message', resp.text)}")
    return data["data"]


def wait_for_payment(reference: str, timeout: int = 120, interval: int = 5) -> dict:
    """Poll until the customer pays, fails, or the timeout passes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tx = verify(reference)
        status = tx.get("status")
        print(f"  status: {status}")
        if status in ("success", "failed", "abandoned", "reversed"):
            return tx
        time.sleep(interval)
    return {"status": "timeout", "reference": reference}


# ---------------------------------------------------------------------------
# Optional webhook: Paystack calls this URL when a payment completes.
# Set the URL in Dashboard -> Settings -> API Keys & Webhooks.
# For local testing, expose it with a tunnel (e.g. ngrok http 5000).
# ---------------------------------------------------------------------------
def run_webhook(port: int = 5000):
    from flask import Flask, request, abort

    app = Flask(__name__)

    @app.post("/paystack/webhook")
    def webhook():
        signature = request.headers.get("x-paystack-signature", "")
        expected = hmac.new(SECRET_KEY.encode(), request.get_data(), hashlib.sha512).hexdigest()
        if not hmac.compare_digest(signature, expected):
            abort(401)  # not from Paystack

        event = request.get_json()
        if event.get("event") == "charge.success":
            d = event["data"]
            print(f"PAID: {d['reference']} | KES {d['amount'] / 100:.2f} | {d.get('customer', {}).get('email')}")
            # TODO: mark the order as paid in your database here
        return "", 200

    app.run(port=port)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "charge":
        if len(sys.argv) < 5:
            sys.exit("Usage: python paystack_stk.py charge <phone> <amount_kes> <email>")
        phone, amount, email = sys.argv[2], float(sys.argv[3]), sys.argv[4]

        result = stk_push(phone, amount, email)
        ref = result["reference"]
        print(f"STK push sent to {format_phone(phone)}. Reference: {ref}")
        inner = result.get("data") or {}
        print(f"Message: {inner.get('display_text') or result.get('message')}")
        print("Ask the customer to enter their M-Pesa PIN...\n")

        final = wait_for_payment(ref)
        if final.get("status") == "success":
            print(f"\n✅ Payment received: KES {final['amount'] / 100:.2f}")
        else:
            print(f"\n❌ Payment not completed (status: {final.get('status')})")

    elif cmd == "verify":
        if len(sys.argv) < 3:
            sys.exit("Usage: python paystack_stk.py verify <reference>")
        tx = verify(sys.argv[2])
        print(f"Status: {tx['status']} | KES {tx['amount'] / 100:.2f} | {tx.get('gateway_response')}")

    elif cmd == "webhook":
        run_webhook()

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
