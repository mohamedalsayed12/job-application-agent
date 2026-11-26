"""The public privacy notice (UK GDPR articles 13 and 14)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.admin_html import page
from app.api.chrome import public_bar

router = APIRouter(tags=["privacy"])

CONTACT_EMAIL = "mohammed33771885@gmail.com"
UPDATED = "22 November 2026"

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
  <li><b>Your email and password.</b> To let you log in. The password is stored only as an Argon2 hash, never as text.
  We email you a link to confirm your address and, if you ask, a link to reset your password; each works once,
  expires within 24 hours (1 hour for a reset), and only a hash of it is stored.</li>
  <li><b>Your profile:</b> target roles, locations, experience levels, travel distance, minimum salary, degrees, skills,
  certifications and licences. To find jobs that fit you and explain each match.</li>
  <li><b>Your CV, if you upload one.</b> Stored so you can download it again. When you upload it, or press "Fill my profile
  from my CV", its text is read to suggest entries for your profile form and your contact details. On the profile page the
  suggestions aren't stored. During sign-up they are kept with your CV only until you finish the last step, so each step
  can show them for you to check, and are then deleted: only what you save is kept. For jobs the fit report marks Apply or Consider, a tailored CV copy (PDF and DOCX) may also be stored
  for you to download; it never replaces your uploaded CV. While a tailored copy is being written, each finished AI
  step is saved so a crash does not repeat it. Those steps are deleted once the copy is finished or has failed, and
  always with the copy itself.</li>
  <li><b>Your job matches:</b> which jobs fit your profile, with a score and the reasons.</li>
  <li><b>Apply answers:</b> legal name, phone, address, work authorisation, visa sponsorship need, links, notice
  period, start date, whether you'd relocate, and full-time or part-time and on-site, hybrid or remote. Used only to
  fill employer forms in your browser.</li>
  <li><b>Diversity answers</b> (optional): gender, ethnic group, disability, armed forces service, free school meals,
  sexual orientation, religion or belief, caring responsibilities, school type and your household's occupation when you
  were 14. Some of these are special category data under UK GDPR. Each defaults to prefer not to say, is never inferred
  from your CV, and is used only to answer the same monitoring questions on employers' forms. Choosing an answer is your
  explicit consent to that use, and you can change it back to prefer not to say at any time.</li>
  <li><b>Prepared applications:</b> which jobs you asked to prepare, the form fields we saw, and the fill plan for your
  extension. Nothing is submitted to an employer by this service.</li>
  <li><b>Extension token</b> (hashed): so the browser extension can download your CV and fill plans.</li>
  <li><b>Outreach contacts and drafts</b>, when you ask the service to find people for a job: names, titles, LinkedIn
  profile URLs, any work emails Apify returned, research snippets, and the messages you edit or send.</li>
  <li><b>Gmail connection</b>, if you connect Gmail: your Gmail address and a sealed refresh token used only to send
  outreach you have approved.</li>
  <li><b>Interview intel requests:</b> which target role you asked us to research, and the resulting practice pack.</li>
  <li><b>Public interview stories</b> from Reddit, YouTube and LinkedIn (short text snippets and URLs only), collected when
  you ask for interview intel on a role. These are not about you; they are used to build a shared findings pack for that role.</li>
  <li><b>Your IP address</b>, held briefly in memory to limit repeated login attempts. It isn't written to the database.</li>
</ul>
<p>The lawful basis is that you asked for this service: we use your data only to provide it to you. Cold email is sent only
when you confirm a draft; you are responsible for having a lawful basis to contact that person under UK PECR.</p>
</div>

<div class="card">
<h2>Who else sees it</h2>
<p>Your data is stored in a database on the operator's own computer. It isn't sold, and it isn't shared except as described
below.</p>
<p>The service uses Anthropic's Claude AI to summarise job adverts and to read your CV when you upload it or ask it to.
So the text of your CV is sent to Anthropic at those times. When you ask for an outreach draft, Claude also receives the
job text, a CV excerpt, and public research snippets about the contact; your login email and name are not sent.</p>
<p>To judge which jobs suit you, the service also sends Anthropic your target roles, experience levels, skills and degrees,
together with each job advert. Your email address, name, locations, salary and CV are not part of that.</p>
<p>To search more widely, the service sends Anthropic each of your target roles on its own and asks which other job
titles employers use for the same work. Nothing else about you is sent with it, and the answer is kept for anyone who
wants the same role.</p>
<p>When you press "Suggest roles and places" on your profile, the service sends Anthropic your skills, degrees, experience
level, target roles, locations and travel distance, so it can suggest job titles and nearby towns to search. Your email
address, name, salary and CV are not part of that.</p>
<p>For each job that first check scores 3.0 or more, the service writes a fuller fit report. For that it sends Anthropic
the job advert with your target roles, experience levels, skills, degrees, salary wish, wanted locations and the text of
your CV, if you uploaded one. Your email address and name are never sent.</p>
<p>When that fit report says Apply or Consider, or when you ask for one, the service may tailor a CV for that job. It
sends Anthropic the job advert and your CV text, so Claude can suggest edits that each quote the CV line they change.
Edits that add a number, a skill or an employer your CV does not show are dropped. Nothing is invented that is not
already in your CV. Your email address is only kept in the tailored file when
it already appears in your uploaded CV.</p>
<p>When you fill an application, the extension in your browser reads the form's questions and sends them to this
service, which answers them from your saved answers. Questions a form asks that your saved answers don't cover are kept
with your account, so you can answer them once and have the same question filled on later forms. For open questions
such as "Why do you want to work here?", the service sends Anthropic the questions, the job advert, your CV text and
your saved education, work and availability answers (never your contact details, right to work, pay, convictions or
diversity answers), so Claude can draft an answer, then sends the drafts back to Claude to
check each one against your CV. Questions about health, disability, identity, age, convictions, immigration, pay and
declarations are never sent: only you answer those. Drafts are outlined on the form for you to read, and this service
never clicks Submit for you.</p>
<p>Finding job listings runs through Apify (LinkedIn and Indeed) and SerpAPI (Google Jobs and company-site search).
The service sends only search queries (role and location), never your email, name or CV.</p>
<p>Finding contacts and public profile research runs through Apify's LinkedIn people actors (not by this app fetching
linkedin.com). Sending approved mail uses Google's Gmail API when you have connected Gmail.</p>
<p>When you ask for interview intel on a target role, the service collects public posts through Apify and sends those
short snippets to Anthropic so it can label success vs failure stories and draft a practice plan. Your email address,
name and CV are not part of that.</p>
</div>

<div class="card">
<h2>Cookies</h2>
<p>One cookie, <code>jaa_session</code>, keeps you logged in for up to 14 days. It's needed for the service to work, so there's
no tracking or advertising cookie to accept or refuse.</p>
</div>

<div class="card">
<h2>How long we keep it</h2>
<p>Until you delete your account. Deleting it on <a href="/account">your account page</a> removes your profile, CV, preferences,
apply answers, applications, extension token, matches, tailored CVs and their unfinished steps, outreach contacts, drafts,
send log and Gmail connection at once.</p>
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
