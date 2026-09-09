# -*- coding: utf-8 -*-
"""
The three emails a client ever receives, as real HTML.

Every one returns (subject, text, html): the plain-text half is not a
throwaway - it is what a client sees in a text-only client, in a Gmail
preview snippet, and in any spam filter that scores the two halves against
each other, so it carries the same information rather than "view this in
HTML".

Written as inlined-style tables on purpose. Gmail strips <style> blocks,
Outlook ignores flexbox and most modern CSS, and neither supports external
stylesheets - so the layout here is deliberately the boring, 2003-looking
kind that actually survives being forwarded.
"""
import html as _html

import settings

DARK = "#0a0a0c"
INK = "#16171b"
SUB = "#6b7080"
LINE = "#e7e8ec"
ACCENT = "#ff9933"
BG = "#f5f6f8"

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif")


def _esc(value):
    return _html.escape(str(value or ""))


def _button(url, label):
    """A bulletproof-ish CTA: a table cell with a background colour, because
    a styled <a> alone renders as bare blue text in several clients."""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:26px 0;"><tr><td align="center" bgcolor="%s" '
        'style="border-radius:8px;">'
        '<a href="%s" style="display:inline-block;padding:14px 30px;font-family:%s;'
        'font-size:15px;font-weight:700;color:%s;text-decoration:none;border-radius:8px;">'
        '%s</a></td></tr></table>' % (ACCENT, _esc(url), FONT, DARK, _esc(label)))


def _shell(title, inner_html, preheader=""):
    """Header band + white card + footer, shared by every email."""
    business = _esc(settings.business_name())
    support = settings.support_email()
    phone = settings.support_phone()
    site = settings.site_url()

    contact_bits = []
    if support:
        contact_bits.append('<a href="mailto:%s" style="color:%s;text-decoration:none;">%s</a>'
                            % (_esc(support), SUB, _esc(support)))
    if phone:
        contact_bits.append(_esc(phone))
    contact = " &nbsp;·&nbsp; ".join(contact_bits)

    legal = ""
    if site:
        base = site.rstrip("/")
        legal = ('<div style="margin-top:10px;">' + " &nbsp;·&nbsp; ".join(
            '<a href="%s/%s" style="color:%s;text-decoration:underline;">%s</a>' % (base, path, SUB, label)
            for path, label in (("terms", "Terms"), ("privacy", "Privacy"),
                                ("refund", "Refund policy"), ("contact", "Contact"))) + '</div>')

    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:%(bg)s;">
<div style="display:none;font-size:1px;color:%(bg)s;max-height:0;overflow:hidden;">%(pre)s</div>
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="background:%(bg)s;padding:28px 12px;">
<tr><td align="center">
  <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:100%%;max-width:560px;">

    <tr><td style="background:%(dark)s;border-radius:14px 14px 0 0;padding:22px 30px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
        <td style="background:%(accent)s;border-radius:7px;width:30px;height:30px;text-align:center;
                   font-family:%(font)s;font-size:14px;font-weight:800;color:%(dark)s;">%(initial)s</td>
        <td style="padding-left:11px;font-family:%(font)s;font-size:15px;font-weight:700;color:#ffffff;">%(business)s</td>
      </tr></table>
    </td></tr>

    <tr><td style="background:#ffffff;padding:34px 30px 30px;font-family:%(font)s;">
      %(inner)s
    </td></tr>

    <tr><td style="background:#ffffff;border-radius:0 0 14px 14px;border-top:1px solid %(line)s;
               padding:20px 30px 26px;font-family:%(font)s;font-size:12px;color:%(sub)s;line-height:1.6;">
      %(contact)s
      %(legal)s
    </td></tr>

  </table>
</td></tr></table>
</body></html>""" % dict(bg=BG, dark=DARK, accent=ACCENT, font=FONT, line=LINE, sub=SUB,
                         business=business, initial=business[:1].upper() or "N",
                         inner=inner_html, contact=contact, legal=legal,
                         pre=_esc(preheader or title))


def _h1(text):
    return ('<h1 style="margin:0 0 14px;font-size:21px;line-height:1.3;color:%s;'
            'font-weight:700;">%s</h1>' % (INK, _esc(text)))


def _p(text, color=None, size=14.5):
    return ('<p style="margin:0 0 14px;font-size:%spx;line-height:1.65;color:%s;">%s</p>'
            % (size, color or SUB, text))


def _panel(rows, highlight_last=False):
    """A bordered key/value block - the plan card, the receipt summary."""
    out = ['<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" '
           'style="border:1px solid %s;border-radius:10px;margin:4px 0 22px;">' % LINE]
    for i, (label, value) in enumerate(rows):
        last = highlight_last and i == len(rows) - 1
        border = "" if i == 0 else "border-top:1px solid %s;" % LINE
        out.append(
            '<tr><td style="%spadding:12px 16px;font-size:13px;color:%s;">%s</td>'
            '<td align="right" style="%spadding:12px 16px;font-size:%spx;font-weight:%s;color:%s;">%s</td></tr>'
            % (border, SUB, _esc(label), border, 16 if last else 13.5,
               700 if last else 600, INK, _esc(value)))
    out.append('</table>')
    return "".join(out)


def _steps(items):
    out = ['<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:2px 0 20px;">']
    for i, text in enumerate(items, 1):
        out.append(
            '<tr><td width="26" valign="top" style="padding:0 0 12px;">'
            '<div style="width:20px;height:20px;border-radius:50%%;background:%s;color:%s;'
            'font-size:11px;font-weight:700;text-align:center;line-height:20px;">%d</div></td>'
            '<td valign="top" style="padding:0 0 12px 8px;font-size:13.5px;line-height:1.55;color:%s;">%s</td></tr>'
            % (ACCENT, DARK, i, SUB, text))
    out.append('</table>')
    return "".join(out)


# ------------------------------------------------------------------ invite
def subscription_invite(client_name, plan_name, amount_rupees, url, product_name=None):
    business = settings.business_name()
    what = product_name or "the work we do for you"
    subject = "Set up your %s subscription with %s" % (plan_name, business)

    text = (
        "Hi %s,\n\n"
        "Here's the link to set up your %s plan with %s - Rs %s per month, covering %s.\n\n"
        "%s\n\n"
        "What happens when you open it:\n"
        "1. You'll confirm the plan and pay securely through Razorpay.\n"
        "2. Razorpay sets up an automatic monthly payment, so you never have to remember it.\n"
        "3. You get an invoice by email after every payment.\n\n"
        "You can cancel any time - just reply to this email.\n\n"
        "%s"
        % (client_name, plan_name, business, amount_rupees, what, url,
           settings.support_email() or ""))

    inner = (
        _h1("Your %s subscription is ready to activate" % plan_name)
        + _p("Hi %s, here's everything set up on our side. One click and you're done." % _esc(client_name))
        + _panel([("Plan", plan_name),
                  ("Covers", what),
                  ("Amount", "Rs %s / month" % amount_rupees)], highlight_last=True)
        + _button(url, "Activate subscription")
        + _p("<strong style='color:%s'>What happens next</strong>" % INK)
        + _steps(["You'll confirm the plan and pay securely through Razorpay.",
                  "Razorpay sets up the monthly payment automatically - nothing to remember each month.",
                  "An invoice lands in your inbox after every payment."])
        + _p("Cancel any time by replying to this email. If the button doesn't work, "
             "paste this into your browser:<br><span style='word-break:break-all;color:%s;'>%s</span>"
             % (SUB, _esc(url)), size=12.5)
    )
    return subject, text, _shell(subject, inner, "Rs %s/month - one click to activate" % amount_rupees)


# ----------------------------------------------------------------- receipt
def payment_receipt(client_name, plan_name, amount_rupees, inv_no, next_billing=None,
                    invoice_url=None):
    business = settings.business_name()
    subject = "Payment received - Invoice %s" % inv_no

    rows = [("Plan", plan_name), ("Invoice number", inv_no)]
    if next_billing:
        rows.append(("Next payment", next_billing))
    rows.append(("Amount paid", "Rs %s" % amount_rupees))

    text = (
        "Hi %s,\n\n"
        "We've received your payment of Rs %s for the %s plan with %s.\n"
        "Your invoice (%s) is attached to this email as a PDF.\n\n"
        "%s"
        "Nothing else is needed from you - your subscription stays active and the next "
        "payment happens automatically.\n\n"
        "Questions? Just reply to this email.\n"
        % (client_name, amount_rupees, plan_name, business, inv_no,
           ("Next payment: %s\n\n" % next_billing) if next_billing else ""))

    inner = (
        '<div style="font-size:13px;font-weight:700;color:#067647;letter-spacing:.04em;'
        'margin:0 0 10px;">PAYMENT SUCCESSFUL</div>'
        + _h1("Thanks, %s - we've received your payment" % client_name)
        + _p("Your invoice is attached to this email as a PDF.")
        + _panel(rows, highlight_last=True)
        + (_button(invoice_url, "Download invoice") if invoice_url else "")
        + _p("Nothing else is needed from you. Your subscription stays active and the next "
             "payment happens automatically on the date above.")
        + _p("Questions about this invoice? Just reply to this email.", size=12.5)
    )
    return subject, text, _shell(subject, inner, "Invoice %s - Rs %s paid" % (inv_no, amount_rupees))


# ------------------------------------------------------------------ failed
def payment_failed(client_name, plan_name):
    business = settings.business_name()
    support = settings.support_email()
    phone = settings.support_phone()
    subject = "Action needed: we couldn't process your %s payment" % plan_name

    contact = support or phone or "us"
    text = (
        "Hi %s,\n\n"
        "This month's payment for your %s plan with %s didn't go through. This usually means "
        "the card expired, the bank declined it, or the account was short at that moment.\n\n"
        "Razorpay will retry automatically. To avoid any interruption to your service, you can "
        "also update your payment method or get in touch with %s and we'll sort it out.\n"
        % (client_name, plan_name, business, contact))

    inner = (
        '<div style="font-size:13px;font-weight:700;color:#b54708;letter-spacing:.04em;'
        'margin:0 0 10px;">PAYMENT FAILED</div>'
        + _h1("We couldn't process this month's payment")
        + _p("Hi %s, this month's payment for your <strong style='color:%s'>%s</strong> plan "
             "didn't go through. That usually means an expired card, a bank decline, or a "
             "temporary shortfall." % (_esc(client_name), INK, _esc(plan_name)))
        + _p("<strong style='color:%s'>What happens now</strong>" % INK)
        + _steps(["Razorpay will retry the payment automatically over the next few days.",
                  "Your service stays running in the meantime.",
                  "If the retries fail too, we'll get in touch before anything is paused."])
        + _p("If you'd rather sort it out now, reply to this email%s and we'll help."
             % (" or call %s" % _esc(phone) if phone else ""))
    )
    return subject, text, _shell(subject, inner, "Your subscription payment needs attention")
