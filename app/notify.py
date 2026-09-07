# -*- coding: utf-8 -*-
"""
Sending a WhatsApp message or an email, through whichever provider is
configured in /admin/settings. Every function here returns (ok, info) and
never raises - a failed notification must not take down a webhook handler
or an admin action; the caller decides whether to surface the failure.
"""
import base64, smtplib, ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

import httpx

import settings


# --------------------------------------------------------------- whatsapp
def send_whatsapp(to_phone, message):
    provider = settings.get("whatsapp_provider")
    to_phone = (to_phone or "").strip()
    if not to_phone:
        return False, "No phone number on file."
    if provider == "meta":
        return _whatsapp_meta(to_phone, message)
    if provider == "twilio":
        return _whatsapp_twilio(to_phone, message)
    return False, "No WhatsApp provider configured."


def _whatsapp_meta(to_phone, message):
    token = settings.get("whatsapp_token")
    phone_id = settings.get("whatsapp_phone_id")
    if not (token and phone_id):
        return False, "WhatsApp (Meta) is not fully configured."
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(
                "https://graph.facebook.com/v19.0/%s/messages" % phone_id,
                headers={"Authorization": "Bearer " + token},
                json={"messaging_product": "whatsapp", "to": to_phone.lstrip("+"),
                      "type": "text", "text": {"body": message}})
        if r.status_code >= 400:
            return False, "WhatsApp send failed: %s" % r.text[:200]
        return True, "sent"
    except Exception as exc:
        return False, "WhatsApp send failed: %s" % exc


def _whatsapp_twilio(to_phone, message):
    sid = settings.get("twilio_account_sid")
    token = settings.get("twilio_auth_token")
    from_number = settings.get("twilio_whatsapp_from")
    if not (sid and token and from_number):
        return False, "WhatsApp (Twilio) is not fully configured."
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(
                "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid,
                auth=(sid, token),
                data={"From": from_number,
                      "To": "whatsapp:" + to_phone if not to_phone.startswith("whatsapp:") else to_phone,
                      "Body": message})
        if r.status_code >= 300:
            return False, "WhatsApp send failed: %s" % r.text[:200]
        return True, "sent"
    except Exception as exc:
        return False, "WhatsApp send failed: %s" % exc


# ------------------------------------------------------------------ email
def send_email(to_email, subject, body):
    to_email = (to_email or "").strip()
    if not to_email:
        return False, "No email address on file."
    if settings.get("resend_api_key"):
        return _email_resend(to_email, subject, body)
    if settings.get("gmail_user") and settings.get("gmail_app_password"):
        return _email_gmail(to_email, subject, body)
    return False, "No email provider configured."


def _email_resend(to_email, subject, body):
    api_key = settings.get("resend_api_key")
    from_addr = settings.get("resend_from_email") or "onboarding@resend.dev"
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post("https://api.resend.com/emails",
                      headers={"Authorization": "Bearer " + api_key},
                      json={"from": from_addr, "to": [to_email], "subject": subject,
                            "text": body})
        if r.status_code >= 300:
            return False, "Email send failed: %s" % r.text[:200]
        return True, "sent"
    except Exception as exc:
        return False, "Email send failed: %s" % exc


def _email_gmail(to_email, subject, body):
    user = settings.get("gmail_user")
    password = settings.get("gmail_app_password")
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_email
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=20) as s:
            s.login(user, password)
            s.sendmail(user, [to_email], msg.as_string())
        return True, "sent"
    except Exception as exc:
        return False, "Email send failed: %s" % exc


def send_email_with_attachment(to_email, subject, body, attachment_bytes, attachment_filename,
                               attachment_mime="application/pdf"):
    """Same provider fallback as send_email(), plus one PDF (or any binary)
    attachment. A separate function rather than an optional param on
    send_email() - most calls need no attachment, and this keeps that path
    untouched."""
    to_email = (to_email or "").strip()
    if not to_email:
        return False, "No email address on file."
    if settings.get("resend_api_key"):
        return _email_resend_attachment(to_email, subject, body, attachment_bytes,
                                        attachment_filename, attachment_mime)
    if settings.get("gmail_user") and settings.get("gmail_app_password"):
        return _email_gmail_attachment(to_email, subject, body, attachment_bytes,
                                       attachment_filename, attachment_mime)
    return False, "No email provider configured."


def _email_resend_attachment(to_email, subject, body, attachment_bytes, filename, mime):
    api_key = settings.get("resend_api_key")
    from_addr = settings.get("resend_from_email") or "onboarding@resend.dev"
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post("https://api.resend.com/emails",
                      headers={"Authorization": "Bearer " + api_key},
                      json={"from": from_addr, "to": [to_email], "subject": subject, "text": body,
                            "attachments": [{"filename": filename,
                                             "content": base64.b64encode(attachment_bytes).decode("ascii")}]})
        if r.status_code >= 300:
            return False, "Email send failed: %s" % r.text[:200]
        return True, "sent"
    except Exception as exc:
        return False, "Email send failed: %s" % exc


def _email_gmail_attachment(to_email, subject, body, attachment_bytes, filename, mime):
    user = settings.get("gmail_user")
    password = settings.get("gmail_app_password")
    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_email
        msg.attach(MIMEText(body))
        part = MIMEApplication(attachment_bytes, _subtype=mime.split("/")[-1])
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=20) as s:
            s.login(user, password)
            s.sendmail(user, [to_email], msg.as_string())
        return True, "sent"
    except Exception as exc:
        return False, "Email send failed: %s" % exc


# --------------------------------------------------------- message bodies
def subscription_link_message(client_name, business, plan_name, amount_rupees, url):
    return (
        "Hi %s, thanks for working with %s!\n\n"
        "To activate your %s plan (₹%s/month), please complete setup here:\n%s\n\n"
        "This authorizes an automatic monthly payment — cancel anytime."
        % (client_name, business, plan_name, amount_rupees, url))


def payment_receipt_message(client_name, business, plan_name, amount_rupees):
    return (
        "Hi %s, your ₹%s payment for the %s plan with %s was received. "
        "Thank you for staying subscribed!" % (client_name, amount_rupees, plan_name, business))


def payment_failed_message(client_name, business, plan_name, support_email, support_phone):
    contact = support_email or support_phone or "us"
    return (
        "Hi %s, we couldn't process this month's payment for your %s plan with %s. "
        "Please update your payment method to avoid interruption, or reach out to %s."
        % (client_name, plan_name, business, contact))
