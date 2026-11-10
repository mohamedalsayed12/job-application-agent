"""The public privacy notice (UK GDPR articles 13 and 14)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.admin_html import page
from app.api.chrome import public_bar

router = APIRouter(tags=["privacy"])

CONTACT_EMAIL = "mohammed33771885@gmail.com"
UPDATED = "10 November 2026"

_BODY = f"""
<h1>Privacy notice</h1>
<p class="muted">Last updated {UPDATED}.</p>

<div class="card">
<h2>Who runs this service</h2>
<p>Job Agent is run by one person as a personal project. That person is the controller of your data.
Contact: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>
</div>

<div class="card">
<h2>What we keep about you, and why</h2>
<ul>
  <li><b>Your email and password.</b> To let you log in. The password is stored only as an Argon2 hash, never as text.</li>
  <li><b>Your profile:</b> target roles, locations, experience level, travel distance, minimum salary, degrees, skills,
  certifications and licences. To find jobs that fit you and explain each match.</li>
  <li><b>Your CV, if you upload one.</b> Stored so you can download it again. When you upload it, or press "Fill my profile
  from my CV", its text is read to suggest entries for your profile form. The suggestions aren't stored: only what you
  save is kept.</li>
  <li><b>Your job matches:</b> which jobs fit your profile, with a score and the reasons.</li>
  <li><b>Your IP address</b>, held briefly in memory to limit repeated login attempts. It isn't written to the database.</li>
</ul>
<p>The lawful basis is that you asked for this service: we use your data only to provide it to you.</p>
</div>

<div class="card">
<h2>Who else sees it</h2>
<p>Your data is stored in a database on the operator's own computer. It isn't sold, and it isn't shared except as described
below.</p>
<p>The service uses Anthropic's Claude AI to summarise job adverts, to read company websites, and to read your CV when you upload
it or ask it to. So the text of your CV is sent to Anthropic at those times.</p>
<p>To judge which jobs suit you, the service also sends Anthropic your target roles, experience level, skills and degrees,
together with each job advert. Your email address, name, locations, salary and CV are not part of that.</p>
</div>

<div class="card">
<h2>Cookies</h2>
<p>One cookie, <code>jaa_session</code>, keeps you logged in for up to 14 days. It's needed for the service to work, so there's
no tracking or advertising cookie to accept or refuse.</p>
</div>

<div class="card">
<h2>How long we keep it</h2>
<p>Until you delete your account. Deleting it on <a href="/account">your account page</a> removes your profile, CV, preferences and
matches at once.</p>
<p>Copies of the database are kept as backups. Only the latest two are kept, and none for more than 30 days, so your data has
left every backup within 30 days of deletion.</p>
</div>

<div class="card">
<h2>Your rights</h2>
<p>You can ask to see, correct, download or delete your data, or object to how it's used. Most of this you can do yourself on your
profile and account pages. For anything else, email <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>.</p>
<p>If you're unhappy with how your data is handled, you can complain to the Information Commissioner's Office at
<a href="https://ico.org.uk/make-a-complaint/" rel="noopener noreferrer">ico.org.uk</a>.</p>
</div>
"""


@router.get("/privacy", response_class=HTMLResponse)
def privacy_notice() -> HTMLResponse:
    return page("Privacy notice", _BODY, header=public_bar(), narrow=True)
