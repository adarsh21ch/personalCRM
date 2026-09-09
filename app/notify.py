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
def send_email(to_email, subject, body, html=None):
    to_email = (to_email or "").strip()
    if not to_email:
        return False, "No email address on file."
    if settings.get("resend_api_key"):
        return _email_resend(to_email, subject, body, html)
    if settings.get("gmail_user") and settings.get("gmail_app_password"):
        return _email_gmail(to_email, subject, body, html)
    return False, "No email provider configured."


def _from_address():
    """What the client sees in their inbox. A bare address reads like a
    machine; 'Nevorai Technologies <billing@...>' reads like a business."""
    addr = settings.get("resend_from_email") or "onboarding@resend.dev"
    business = settings.business_name()
    if business and "<" not in addr:
        return '%s <%s>' % (business, addr)
    return addr


def _email_resend(to_email, subject, body, html=None):
    api_key = settings.get("resend_api_key")
    payload = {"from": _from_address(), "to": [to_email], "subject": subject, "text": body}
    if html:
        payload["html"] = html
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post("https://api.resend.com/emails",
                      headers={"Authorization": "Bearer " + api_key}, json=payload)
        if r.status_code >= 300:
            return False, "Email send failed: %s" % r.text[:200]
        return True, "sent"
    except Exception as exc:
        return False, "Email send failed: %s" % exc


def _gmail_message(user, to_email, subject, body, html=None):
    """text-only, or a multipart/alternative carrying both halves. The text
    part goes first: mail clients render the LAST part they understand, so
    ordering it text-then-html is what makes the HTML win where supported
    and the text show where it isn't."""
    business = settings.business_name()
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = ('%s <%s>' % (business, user)) if business else user
    msg["To"] = to_email
    return msg


def _email_gmail(to_email, subject, body, html=None):
    user = settings.get("gmail_user")
    password = settings.get("gmail_app_password")
    try:
        msg = _gmail_message(user, to_email, subject, body, html)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=20) as s:
            s.login(user, password)
            s.sendmail(user, [to_email], msg.as_string())
        return True, "sent"
    except Exception as exc:
        return False, "Email send failed: %s" % exc


def send_email_with_attachment(to_email, subject, body, attachment_bytes, attachment_filename,
                               attachment_mime="application/pdf", html=None):
    """Same provider fallback as send_email(), plus one PDF (or any binary)
    attachment. A separate function rather than an optional param on
    send_email() - most calls need no attachment, and this keeps that path
    untouched."""
    to_email = (to_email or "").strip()
    if not to_email:
        return False, "No email address on file."
    if settings.get("resend_api_key"):
        return _email_resend_attachment(to_email, subject, body, attachment_bytes,
                                        attachment_filename, attachment_mime, html)
    if settings.get("gmail_user") and settings.get("gmail_app_password"):
        return _email_gmail_attachment(to_email, subject, body, attachment_bytes,
                                       attachment_filename, attachment_mime, html)
    return False, "No email provider configured."


def _email_resend_attachment(to_email, subject, body, attachment_bytes, filename, mime, html=None):
    api_key = settings.get("resend_api_key")
    payload = {"from": _from_address(), "to": [to_email], "subject": subject, "text": body,
               "attachments": [{"filename": filename,
                                "content": base64.b64encode(attachment_bytes).decode("ascii")}]}
    if html:
        payload["html"] = html
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post("https://api.resend.com/emails",
                      headers={"Authorization": "Bearer " + api_key}, json=payload)
        if r.status_code >= 300:
            return False, "Email send failed: %s" % r.text[:200]
        return True, "sent"
    except Exception as exc:
        return False, "Email send failed: %s" % exc


def _email_gmail_attachment(to_email, subject, body, attachment_bytes, filename, mime, html=None):
    user = settings.get("gmail_user")
    password = settings.get("gmail_app_password")
    try:
        # mixed(alternative(text, html), attachment) - the nesting matters:
        # a flat multipart with an html part beside a PDF makes some clients
        # show the attachment and drop the body.
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = ('%s <%s>' % (settings.business_name(), user)) if settings.business_name() else user
        msg["To"] = to_email
        if html:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(body, "plain", "utf-8"))
            alt.attach(MIMEText(html, "html", "utf-8"))
            msg.attach(alt)
        else:
            msg.attach(MIMEText(body, "plain", "utf-8"))
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


# --------------------------------------------------------- WhatsApp bodies
# Short, plain, and skimmable on a phone. The long-form, formatted versions
# of all three of these live in emails.py - WhatsApp is the nudge, email is
# the record.
def subscription_link_message(client_name, business, plan_name, amount_rupees, url):
    return (
        "Hi %s 👋\n\n"
        "Your %s plan with %s is ready to activate — ₹%s/month.\n\n"
        "Set it up here (takes a minute):\n%s\n\n"
        "You'll pay securely via Razorpay, and it renews automatically each month so you "
        "never have to remember it. An invoice reaches your email after every payment, and "
        "you can cancel any time."
        % (client_name, plan_name, business, amount_rupees, url))


def payment_receipt_message(client_name, business, plan_name, amount_rupees, inv_no=None):
    return (
        "Hi %s ✅\n\n"
        "We've received your ₹%s payment for the %s plan with %s.%s\n\n"
        "Your invoice has been emailed to you. Nothing else needed — the next payment "
        "happens automatically. Thank you!"
        % (client_name, amount_rupees, plan_name, business,
           ("\nInvoice: %s" % inv_no) if inv_no else ""))


def payment_failed_message(client_name, business, plan_name, support_email, support_phone):
    contact = support_email or support_phone or "us"
    return (
        "Hi %s,\n\n"
        "This month's payment for your %s plan with %s didn't go through — usually an "
        "expired card or a bank decline.\n\n"
        "Razorpay will retry automatically over the next few days and your service keeps "
        "running meanwhile. To sort it out now, reply here or contact %s."
        % (client_name, plan_name, business, contact))
