"""The extra questions employers' forms ask (D74), with the answer choices in the wording UK forms use.

One list drives three things: the form the person fills in once, what is saved (the `extra` JSON on
their answers row), and how a field on an employer's form is recognised (`patterns`). Choices follow
the 2021 Census (ethnic group, religion, sexual orientation, marital status), the Social Mobility
Commission (parents' education) and the Home Office's names for kinds of permission to work.
An answer is never guessed: a blank one is left for the person on the employer's form.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import PREFER_NOT

YES_NO = ("Yes", "No")


@dataclass(frozen=True, slots=True)
class Question:
    key: str
    label: str
    group: str
    # choice, yes_no, text, date or month
    kind: str = "text"
    choices: tuple[str, ...] = ()
    # Regexes matched against an employer's field label (lower case). More specific questions come first.
    patterns: tuple[str, ...] = ()
    hint: str = ""
    # Diversity questions start at prefer not to say; every other one starts blank.
    prefer_not: bool = False


RIGHT_TO_WORK_TYPES = (
    "British or Irish citizen",
    "Settled status (EU Settlement Scheme)",
    "Pre-settled status (EU Settlement Scheme)",
    "Indefinite leave to remain",
    "Graduate visa",
    "Student visa",
    "Skilled Worker visa",
    "Youth Mobility Scheme visa",
    "Dependant or family visa",
    "Other visa or permission",
    "I do not have the right to work in the UK",
)
TITLES = ("Mr", "Mrs", "Miss", "Ms", "Mx", "Dr", PREFER_NOT)
PRONOUNS = ("He/him", "She/her", "They/them", "Prefer to self-describe", PREFER_NOT)
QUALIFICATION_LEVELS = (
    "Doctorate (PhD)",
    "Master's degree",
    "Bachelor's degree",
    "Foundation degree",
    "HND or HNC",
    "A levels or equivalent",
    "GCSEs or equivalent",
    "No formal qualifications",
)
DEGREE_GRADES = (
    "First class (1st)",
    "Upper second class (2:1)",
    "Lower second class (2:2)",
    "Third class (3rd)",
    "Pass",
    "Distinction",
    "Merit",
    "Not yet awarded",
)
YEARS_EXPERIENCE = ("None", "Less than 1 year", "1 to 2 years", "3 to 5 years", "6 to 10 years", "More than 10 years")
HEARD_ABOUT = (
    "LinkedIn",
    "Indeed",
    "Company website",
    "Job board",
    "Google",
    "Recruitment agency",
    "Referral from an employee",
    "University careers service",
    "Careers fair or event",
    "Social media",
    "Other",
)
DRIVING_LICENCES = (
    "Full UK driving licence",
    "Provisional UK driving licence",
    "Full non-UK driving licence",
    "No driving licence",
)
SECURITY_CLEARANCES = ("None", "BPSS", "CTC", "SC", "DV")
AGE_BANDS = ("16 to 24", "25 to 34", "35 to 44", "45 to 54", "55 to 64", "65 or over", PREFER_NOT)
MARITAL_STATUSES = (
    "Never married and never registered a civil partnership",
    "Married",
    "In a registered civil partnership",
    "Separated",
    "Divorced or civil partnership dissolved",
    "Widowed or surviving civil partner",
    PREFER_NOT,
)
PARENTS_UNIVERSITY = (
    "No, neither of my parents attended university",
    "Yes, one or both of my parents attended university",
    "I don't know",
    PREFER_NOT,
)
GENDER_SAME_AS_BIRTH = ("Yes", "No", PREFER_NOT)

EXTRA_QUESTIONS: tuple[Question, ...] = (
    # Personal details
    Question("title", "Title", "details", "choice", TITLES, (r"^title\b", r"\bsalutation\b")),
    Question(
        "preferred_name",
        "Preferred name",
        "details",
        patterns=(r"preferred (first )?name", r"known as", r"\bnickname\b"),
        hint="if different from your legal first name",
    ),
    Question("pronouns", "Pronouns", "details", "choice", PRONOUNS, (r"\bpronouns?\b",)),
    Question(
        "date_of_birth",
        "Date of birth",
        "details",
        "date",
        patterns=(r"date of birth", r"\bdob\b", r"birth ?date"),
        hint="only a few forms ask, such as ones with security checks",
    ),
    Question("address_line_2", "Address line 2", "details", patterns=(r"address (line )?2", r"apartment|flat number|suite")),
    Question("county", "County", "details", patterns=(r"\bcounty\b", r"state or province", r"\bprovince\b")),
    # Right to work
    Question(
        "right_to_work_type",
        "What gives you the right to work in the UK?",
        "right_to_work",
        "choice",
        RIGHT_TO_WORK_TYPES,
        (
            r"(type|basis|kind|nature) of (your )?(right to work|visa|work permit|permission)",
            r"immigration status",
            r"visa (type|status|category)",
            r"what (visa|permission)",
            r"current (visa|work permit)",
        ),
    ),
    Question(
        "visa_expiry_date",
        "When does your visa or permission end?",
        "right_to_work",
        "date",
        patterns=(r"(visa|permit|permission|leave to remain).{0,30}(expir|end|valid until)", r"expiry date.{0,20}(visa|permit)"),
        hint="leave blank if it has no end date",
    ),
    Question(
        "uk_resident_5_years",
        "Have you lived in the UK for the last 5 years?",
        "right_to_work",
        "yes_no",
        patterns=(r"(lived|resident|resided|residency).{0,40}(5|five|3|three) years", r"(5|five) years.{0,30}(uk|united kingdom) resid"),
        hint="asked for roles that need security clearance",
    ),
    Question(
        "security_clearance",
        "Security clearance you hold now",
        "right_to_work",
        "choice",
        SECURITY_CLEARANCES,
        (r"(hold|current|existing|level of).{0,30}(security )?clearance", r"security clearance (level|held)"),
    ),
    Question(
        "willing_security_clearance",
        "Are you willing to go through security clearance?",
        "right_to_work",
        "yes_no",
        patterns=(r"(willing|able|eligible|happy|prepared).{0,40}(security )?(clearance|vetting)", r"background check"),
    ),
    # Education
    Question(
        "highest_qualification",
        "Highest level of education",
        "education",
        "choice",
        QUALIFICATION_LEVELS,
        (r"highest (level of )?(education|qualification)", r"level of education", r"education level", r"qualification level"),
    ),
    Question(
        "university",
        "University or college",
        "education",
        patterns=(r"\buniversity\b", r"\binstitution\b", r"school or university", r"\bcollege\b", r"school name"),
    ),
    Question(
        "degree_title",
        "Degree or course",
        "education",
        patterns=(r"\bdegree\b(?!.{0,20}(class|grade|result))", r"course (title|name)", r"qualification (title|name)", r"field of study", r"\bmajor\b", r"\bdiscipline\b"),
        hint="for example BSc Business Management",
    ),
    Question(
        "degree_grade",
        "Degree grade, achieved or predicted",
        "education",
        "choice",
        DEGREE_GRADES,
        (r"degree (class|classification|grade|result)", r"(predicted|expected|achieved|final) (grade|classification|result)", r"\bclassification\b", r"\bgpa\b"),
    ),
    Question(
        "graduation_date",
        "Graduation month, past or expected",
        "education",
        "month",
        patterns=(r"graduat(ion|e|ing) (date|year|month)", r"(expected|anticipated) graduation", r"year of graduation", r"when (did|will) you graduate", r"completion date"),
    ),
    Question(
        "currently_studying",
        "Are you still studying?",
        "education",
        "yes_no",
        patterns=(r"(currently|still) (studying|a student|in education|enrolled)",),
    ),
    Question(
        "a_levels",
        "A levels or equivalent, with grades",
        "education",
        patterns=(r"\ba[- ]?levels?\b", r"level 3 qualification"),
        hint="for example Maths A, Business B, Economics B",
    ),
    Question("ucas_points", "UCAS points", "education", patterns=(r"ucas (tariff )?points", r"\bucas\b")),
    Question(
        "gcse_maths",
        "GCSE Maths grade",
        "education",
        patterns=(r"gcse.{0,20}math", r"math.{0,20}gcse"),
        hint="for example 7 or A",
    ),
    Question(
        "gcse_english",
        "GCSE English grade",
        "education",
        patterns=(r"gcse.{0,20}english", r"english.{0,20}gcse"),
        hint="for example 6 or B",
    ),
    # Work
    Question(
        "currently_employed",
        "Are you working at the moment?",
        "work",
        "yes_no",
        patterns=(r"currently (employed|working)", r"are you (employed|in employment)"),
    ),
    Question(
        "current_job_title",
        "Current or most recent job title",
        "work",
        patterns=(r"(current|most recent|present|latest) (job )?(title|role|position)", r"^job title\b"),
    ),
    Question(
        "current_employer",
        "Current or most recent employer",
        "work",
        patterns=(r"(current|most recent|present|latest) (employer|company|organi[sz]ation)", r"^(employer|company)( name)?\b"),
    ),
    Question(
        "years_experience",
        "Years of relevant work experience",
        "work",
        "choice",
        YEARS_EXPERIENCE,
        (r"years of (relevant |professional |work |commercial )*experience", r"how many years.{0,40}experience", r"total experience"),
    ),
    Question(
        "salary_expectation",
        "Salary you expect",
        "work",
        patterns=(r"(salary|pay|compensation|remuneration) (expectation|requirement|desired|sought)", r"(expected|desired|target) (salary|pay|compensation)", r"salary expectations?"),
        hint="a year, in pounds, for example 30000",
    ),
    Question(
        "current_salary",
        "Current salary",
        "work",
        patterns=(r"(current|present|most recent) (salary|pay|compensation|remuneration)",),
        hint="leave blank to answer it yourself each time",
    ),
    Question("languages", "Languages you speak", "work", patterns=(r"languages? (spoken|you speak|skills|proficiency)", r"^languages?\b")),
    # Common screening questions
    Question(
        "heard_about_source",
        "Where you usually find jobs",
        "screening",
        "choice",
        HEARD_ABOUT,
        (r"how did you (hear|find|learn)", r"how you heard", r"where did you (hear|see|find)", r"source of application", r"referral source"),
        hint="used for 'How did you hear about us?'",
    ),
    Question(
        "over_18",
        "Are you 18 or over?",
        "screening",
        "yes_no",
        patterns=(r"(18|eighteen)( years)?( of age)? or (over|older)", r"over (the age of )?18", r"at least 18"),
    ),
    Question(
        "criminal_convictions",
        "Do you have any unspent criminal convictions?",
        "screening",
        "yes_no",
        patterns=(r"unspent", r"criminal (conviction|record|offence)", r"\bconvictions?\b", r"convicted of"),
    ),
    Question(
        "driving_licence",
        "Driving licence",
        "screening",
        "choice",
        DRIVING_LICENCES,
        (r"driving licen[cs]e", r"driver'?s licen[cs]e", r"\bdo you drive\b"),
    ),
    Question(
        "own_vehicle",
        "Do you have your own vehicle?",
        "screening",
        "yes_no",
        patterns=(r"(own|access to) (a |your own )?(car|vehicle|transport)",),
    ),
    Question(
        "willing_to_travel",
        "Are you willing to travel for work?",
        "screening",
        "yes_no",
        patterns=(r"(willing|able|happy|prepared) to travel", r"travel (requirement|up to)"),
    ),
    Question(
        "reasonable_adjustments",
        "Do you need any adjustments for the application or interview?",
        "screening",
        "yes_no",
        # Not the follow-up that asks for the details.
        patterns=(r"^(?!.*(details|describe|tell us)).*(reasonable adjustments?|(adjustments?|accommodations?).{0,40}(interview|recruitment|application|assessment))",),
    ),
    Question(
        "adjustments_details",
        "The adjustments you need",
        "screening",
        patterns=(r"(details|describe|tell us).{0,40}adjustments?",),
        hint="only if you answered yes",
    ),
    Question(
        "relatives_at_company",
        "Do you usually have friends or relatives at the companies you apply to?",
        "screening",
        "yes_no",
        patterns=(r"(related to|relatives?|family members?|personal relationship).{0,60}(employee|work|company|organi[sz]ation)", r"conflicts? of interest"),
        hint="most people answer no; change it on the form when it differs",
    ),
    Question(
        "non_compete",
        "Are you bound by a non-compete or other restriction?",
        "screening",
        "yes_no",
        patterns=(r"non[- ]?compete", r"restrictive covenants?", r"(contractual|post[- ]employment) restrictions?"),
    ),
    Question(
        "agree_privacy_notice",
        "Tick 'I have read the privacy notice' boxes for me",
        "screening",
        "yes_no",
        patterns=(r"(read|agree|accept|consent|acknowledge).{0,60}(privacy|data protection|gdpr)", r"(privacy|data protection) (notice|policy|statement)"),
        hint="you still check the form before you submit it",
    ),
    Question(
        "talent_pool_consent",
        "Let employers keep your details for future roles?",
        "screening",
        "yes_no",
        patterns=(r"talent (pool|community|network)", r"(keep|retain|hold).{0,40}(details|data|cv|information).{0,40}(future|other) (roles?|opportunit|vacanc)", r"future (opportunities|vacancies|roles)"),
    ),
    Question(
        "marketing_consent",
        "Receive marketing emails from employers?",
        "screening",
        "yes_no",
        patterns=(r"marketing (emails?|communications?|messages?|consent)", r"\bnewsletters?\b", r"(job|career) alerts?"),
    ),
    # More diversity questions
    Question(
        "gender_same_as_birth",
        "Is the gender you identify with the same as your sex registered at birth?",
        "diversity",
        "choice",
        GENDER_SAME_AS_BIRTH,
        (r"(same|different).{0,60}(registered|assigned) at birth", r"sex (registered|assigned) at birth", r"\btransgender\b", r"\btrans\b", r"gender reassignment"),
        prefer_not=True,
    ),
    Question(
        "age_band",
        "Age group",
        "diversity",
        "choice",
        AGE_BANDS,
        (r"age (group|band|range|bracket|category)", r"^(your )?age\b", r"how old are you"),
        prefer_not=True,
    ),
    Question(
        "marital_status",
        "Marital or civil partnership status",
        "diversity",
        "choice",
        MARITAL_STATUSES,
        (r"marital", r"civil partnership status", r"relationship status"),
        prefer_not=True,
    ),
    Question(
        "parents_university",
        "Did either of your parents attend university by the time you were 18?",
        "diversity",
        "choice",
        PARENTS_UNIVERSITY,
        (r"parents?.{0,60}(university|degree|higher education)", r"first (in|generation).{0,40}(family|university)"),
        prefer_not=True,
    ),
)

EXTRA_BY_KEY = {question.key: question for question in EXTRA_QUESTIONS}
GROUP_TITLES = {
    "details": "More about you",
    "right_to_work": "Right to work",
    "education": "Education",
    "work": "Work experience and pay",
    "screening": "Questions most forms ask",
    "diversity": "More diversity questions",
}


def questions_in(group: str) -> tuple[Question, ...]:
    return tuple(question for question in EXTRA_QUESTIONS if question.group == group)
