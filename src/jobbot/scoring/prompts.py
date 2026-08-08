"""Prompt assembly for scoring.

Layout is dictated by prompt caching. The rendered order is
``system`` then ``messages``, and caching is a *prefix* match — so:

    system[0] = RUBRIC      (identical for every user, every run)
    system[1] = resume      (identical for every job)  <- cache breakpoint
    messages  = job description (varies per request)

Nothing above the breakpoint may vary. No timestamps, no job IDs, no
per-request interpolation anywhere in the system blocks — a single changed
byte silently invalidates the prefix and every call pays full price.
"""

from __future__ import annotations

RUBRIC = """\
You are screening job postings on behalf of one candidate. You will be given \
that candidate's resume, then a single job description. Decide how well the \
candidate matches this specific role.

Score 0-100 on this scale:

  85-100  Strong. Meets essentially every stated requirement, and the core \
technologies and domain overlap heavily with the candidate's actual, \
demonstrated experience. They would likely clear a screen.
  65-84   Possible. Meets most requirements. Any gaps are learnable on the \
job or peripheral to the role's core work.
  40-64   Weak. Meaningful gaps in the primary skills or seniority the role \
asks for. Applying is a long shot.
  0-39    Poor. Different discipline, wildly different seniority, or the \
posting asks for a stack the candidate has never worked in.

Set `verdict` to match the band: strong (85+), possible (65-84), weak (40-64). \
Use `disqualified` ONLY when a hard blocker exists, regardless of score.

Hard blockers — list these in `blockers`, and only these:
  - Work authorization the candidate does not have, or sponsorship the \
posting explicitly refuses to provide.
  - A location requirement the candidate cannot meet, where the role is not \
remote.
  - A required credential, clearance, certification, or degree the candidate \
plainly lacks.
  - A minimum years-of-experience bar the candidate misses by more than three \
years.

An unmet preferred qualification is a gap, never a blocker. If nothing \
disqualifies the candidate, `blockers` must be empty.

Rules for the evidence you cite:

  - `strengths` must quote or closely paraphrase something actually present \
in the resume. Do not credit the candidate with a skill because the job wants \
it, because a related tool appears, or because it seems likely. If you cannot \
point at the resume, it is not a strength.
  - `gaps` are requirements stated in the posting with no support in the \
resume. Be specific: "no Kubernetes experience shown" beats "some gaps".
  - Judge against what the posting actually requires, not against an ideal \
candidate. Postings routinely overstate; weight the responsibilities section \
above the wish list.
  - Years of experience: compare to the resume's actual timeline. Do not \
assume seniority from job titles alone.
  - If the description is truncated, empty, or is not a job posting at all, \
score it 0, set verdict to "weak", and say so in `gaps`.

`tailored_summary`: 2-3 sentences positioning this candidate for this \
specific role, suitable for pasting into a "why are you a fit" field. Draw \
only on the resume. State no fact about the candidate that the resume does \
not support. No greeting, no sign-off, no placeholders.

Be calibrated rather than generous. This score decides whether a human spends \
time on an application, so an inflated score wastes their day and a deflated \
one costs them an opportunity.\
"""


def system_blocks(resume_text: str, *, cache: bool = True) -> list[dict]:
    """The cacheable prefix: rubric + resume.

    The breakpoint goes on the *last* block so tools+system cache together.
    """
    blocks: list[dict] = [
        {"type": "text", "text": RUBRIC},
        {"type": "text", "text": f"<resume>\n{resume_text}\n</resume>"},
    ]
    if cache:
        # 1h TTL: comfortably outlives a scoring run, so every job after the
        # first reads the resume from cache at ~0.1x input price.
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    return blocks


def user_message(
    *,
    title: str,
    company: str,
    location: str,
    description: str,
    max_chars: int = 24_000,
) -> str:
    """The per-job turn. Everything variable lives here, after the breakpoint."""
    body = description.strip()
    if len(body) > max_chars:
        # Requirements cluster at the top and bottom of a posting; the middle
        # is usually boilerplate about the company.
        head = body[: int(max_chars * 0.7)]
        tail = body[-int(max_chars * 0.3) :]
        body = f"{head}\n\n[...truncated...]\n\n{tail}"

    return (
        "<job>\n"
        f"<title>{title}</title>\n"
        f"<company>{company}</company>\n"
        f"<location>{location or 'not stated'}</location>\n"
        f"<description>\n{body}\n</description>\n"
        "</job>\n\n"
        "Score this posting against the resume."
    )
