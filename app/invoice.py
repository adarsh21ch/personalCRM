# -*- coding: utf-8 -*-
"""
One PDF invoice per successful charge, generated fresh at send time - no
storage bucket, no persisted invoice number counter, nothing that can drift
out of sync with the payment it describes. The invoice number is derived
from the payment row itself (its id and month), so it's stable and unique
without any shared counter state to get out of sync across concurrent
webhook deliveries.

GST: plan prices are charged exactly as shown - nothing is added at
checkout - so a price is GST-INCLUSIVE by construction. With a GSTIN
configured, this backs the tax out of that fixed total for the legal
breakup; without one, the invoice shows a plain amount and no tax line,
because charging a tax you are not registered for is not something to do
by accident.
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas

import settings


def invoice_number(payment):
    return "INV-%s-%s" % (payment["created_at"][:7].replace("-", ""), payment["id"][:6].upper())


def _gst_breakup(amount_paise, rate_percent):
    """(base_paise, gst_paise) backed out of a GST-inclusive total."""
    rate = rate_percent / 100.0
    base = round(amount_paise / (1 + rate))
    return base, amount_paise - base


def build_invoice_pdf(payment, client, plan):
    """PDF bytes for one payment. Never raises on missing optional settings -
    a blank business address or unset GSTIN just means a shorter invoice,
    not a failure to send the receipt at all."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    ink = colors.HexColor("#16171b")
    sub = colors.HexColor("#6b7080")
    accent = colors.HexColor("#4f46e5")
    line = colors.HexColor("#e7e8ec")

    business = settings.business_name()
    address = settings.business_address()
    gstin = settings.gstin()
    inv_no = invoice_number(payment)
    date_str = payment["created_at"][:10]
    amount = payment["amount_paise"]

    y = H - 22 * mm
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(20 * mm, y, business)
    c.setFont("Helvetica", 9)
    c.setFillColor(sub)
    y -= 6 * mm
    if address:
        for part in address.split("\n")[:3]:
            c.drawString(20 * mm, y, part.strip())
            y -= 4.5 * mm
    if gstin:
        c.drawString(20 * mm, y, "GSTIN: %s" % gstin)
        y -= 4.5 * mm

    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(W - 20 * mm, H - 22 * mm, "INVOICE")
    c.setFont("Helvetica", 9)
    c.setFillColor(sub)
    c.drawRightString(W - 20 * mm, H - 28 * mm, "No. %s" % inv_no)
    c.drawRightString(W - 20 * mm, H - 33 * mm, "Date: %s" % date_str)

    y = H - 48 * mm
    c.setStrokeColor(line)
    c.line(20 * mm, y, W - 20 * mm, y)

    y -= 10 * mm
    c.setFillColor(sub)
    c.setFont("Helvetica", 8.5)
    c.drawString(20 * mm, y, "BILLED TO")
    y -= 5.5 * mm
    c.setFillColor(ink)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, client.get("name") or "")
    y -= 5 * mm
    c.setFont("Helvetica", 9.5)
    c.setFillColor(sub)
    if client.get("company"):
        c.drawString(20 * mm, y, client["company"])
        y -= 4.5 * mm
    if client.get("email"):
        c.drawString(20 * mm, y, client["email"])
        y -= 4.5 * mm

    y -= 10 * mm
    table_top = y
    c.setFillColor(colors.HexColor("#f5f6f8"))
    c.rect(20 * mm, y - 8 * mm, W - 40 * mm, 8 * mm, fill=1, stroke=0)
    c.setFillColor(sub)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(22 * mm, y - 5.5 * mm, "DESCRIPTION")
    c.drawRightString(W - 22 * mm, y - 5.5 * mm, "AMOUNT")
    y -= 8 * mm

    row_h = 9 * mm
    plan_name = plan.get("name") if plan else "Subscription"

    def rupees(paise):
        return "Rs. %s" % format(paise / 100.0, ",.2f")

    if gstin:
        rate = settings.gst_rate_percent()
        base_paise, gst_paise = _gst_breakup(amount, rate)
        rows = [
            ("%s - monthly subscription" % plan_name, rupees(base_paise)),
            ("GST (%.0f%%, included above)" % rate, rupees(gst_paise)),
        ]
    else:
        rows = [("%s - monthly subscription" % plan_name, rupees(amount))]

    c.setFont("Helvetica", 9.5)
    for label, value in rows:
        y -= row_h
        c.setFillColor(ink)
        c.drawString(22 * mm, y + 2 * mm, label)
        c.drawRightString(W - 22 * mm, y + 2 * mm, value)
        c.setStrokeColor(line)
        c.line(20 * mm, y, W - 20 * mm, y)

    y -= row_h
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(accent)
    c.drawString(22 * mm, y + 2 * mm, "TOTAL PAID")
    c.drawRightString(W - 22 * mm, y + 2 * mm, rupees(amount))

    c.setFillColor(sub)
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, 18 * mm, "Paid via Razorpay. This invoice confirms a payment already received - it is not a request for payment.")
    email = settings.support_email()
    if email:
        c.drawString(20 * mm, 14 * mm, "Questions about this invoice: %s" % email)

    c.showPage()
    c.save()
    return buf.getvalue()
