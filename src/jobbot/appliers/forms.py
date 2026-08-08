"""Reading and filling application forms.

Fields are extracted in a single `page.evaluate` rather than by walking
Playwright locators. One round trip instead of dozens, and label resolution
(`<label for>`, `aria-label`, wrapping label, placeholder, preceding text) is
far easier in the DOM than through the automation API.

Filling is deliberately separate from reading: every field is resolved to an
answer *before* anything is typed, so an application either fills completely
or parks without having half-submitted itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from .answers import Question

# Extracts every user-fillable control in `root`, with its label, kind,
# requiredness, and options. Returns a stable `sel` for filling later.
_EXTRACT_JS = """
(rootSelector) => {
  const root = rootSelector ? document.querySelector(rootSelector) : document;
  if (!root) return [];

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };

  const CONTROLS = 'input:not([type=hidden]):not([type=submit]):not([type=button])'
                 + ':not([type=reset]), textarea, select';

  // job_application[answers][expected_ctc] -> "Expected ctc"
  const humanize = (name) => {
    if (!name) return '';
    const m = name.match(/\\[([^\\[\\]]+)\\]\\s*$/);
    const s = (m ? m[1] : name).replace(/[_\\-]+/g, ' ').trim();
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
  };

  const legendFor = (el) => {
    const fs = el.closest('fieldset');
    if (!fs) return '';
    if (fs.getAttribute('aria-label')) return fs.getAttribute('aria-label').trim();
    const lg = fs.querySelector('legend');
    return lg && lg.innerText.trim() ? lg.innerText.trim() : '';
  };

  const labelFor = (el) => {
    // 1. explicit <label for="id">
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    // 2. aria-label / aria-labelledby
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const ariaBy = el.getAttribute('aria-labelledby');
    if (ariaBy) {
      const t = ariaBy.split(/\\s+/).map(id => document.getElementById(id))
                      .filter(Boolean).map(n => n.innerText.trim()).join(' ');
      if (t) return t;
    }
    // 3. wrapping <label>
    const wrap = el.closest('label');
    if (wrap && wrap.innerText.trim()) return wrap.innerText.trim();
    // 4. a labelish ancestor — but ONLY while that ancestor contains this one
    //    control. Once it holds siblings, any label inside it belongs to one
    //    of them, and grabbing it would mislabel this field (which in turn
    //    would answer the wrong question on a real application).
    let node = el.parentElement;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      if (node.querySelectorAll(CONTROLS).length > 1) break;
      const cand = node.querySelector('label, legend, .label, [class*="label"], [class*="question"]');
      if (cand && cand.innerText.trim()) return cand.innerText.trim();
    }
    // 5. last resort: placeholder, then a humanized field name
    return (el.placeholder || '').trim() || humanize(el.name);
  };

  const selectorFor = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(el.name)}"]`;
    const all = Array.from(root.querySelectorAll(el.tagName.toLowerCase()));
    return `${el.tagName.toLowerCase()}:nth-of-type(${all.indexOf(el) + 1})`;
  };

  const SKIP = new Set(['hidden', 'submit', 'button', 'reset', 'image', 'search']);
  const out = [];
  const seenRadioGroups = new Set();

  for (const el of root.querySelectorAll('input, textarea, select')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.type || 'text').toLowerCase();
    if (tag === 'input' && SKIP.has(type)) continue;
    if (el.disabled || el.readOnly) continue;
    if (type !== 'file' && !visible(el)) continue;

    // Radios share one question; emit the group once.
    if (type === 'radio') {
      const key = el.name || labelFor(el);
      if (seenRadioGroups.has(key)) continue;
      seenRadioGroups.add(key);
      const group = Array.from(root.querySelectorAll(
        `input[type="radio"][name="${CSS.escape(el.name || '')}"]`));
      // A radio group's question is its <legend>, never a neighbouring
      // <label> — those belong to the individual options.
      out.push({
        label: legendFor(el) || humanize(el.name) || key,
        kind: 'choice',
        required: el.required || el.getAttribute('aria-required') === 'true',
        options: group.map(r => (labelFor(r) || r.value || '').trim()).filter(Boolean),
        sel: el.name ? `input[type="radio"][name="${CSS.escape(el.name)}"]` : selectorFor(el),
        name: el.name || '',
        value: '',
      });
      continue;
    }

    let kind = 'text';
    if (tag === 'textarea') kind = 'text';
    else if (tag === 'select') kind = 'choice';
    else if (type === 'checkbox') kind = 'bool';
    else if (type === 'file') kind = 'file';
    else if (type === 'number') kind = 'number';
    else if (type === 'email') kind = 'email';
    else if (type === 'tel') kind = 'phone';
    else if (type === 'url') kind = 'url';

    const options = tag === 'select'
      ? Array.from(el.options).map(o => o.text.trim()).filter(t => t && !/^select/i.test(t))
      : [];

    const label = labelFor(el);
    if (!label && kind !== 'file') continue;  // unlabelled and unknowable

    out.push({
      label,
      kind,
      required: el.required || el.getAttribute('aria-required') === 'true'
                || /\\*/.test(label),
      options,
      sel: selectorFor(el),
      name: el.name || '',
      value: el.value || '',
    });
  }
  return out;
}
"""


@dataclass
class FormField:
    """One control on a form, plus how to find it again."""

    question: Question
    selector: str
    kind: str
    options: list[str]
    name: str = ""

    @property
    def is_file(self) -> bool:
        return self.kind == "file"


def _clean_label(label: str) -> str:
    """Strip the required-marker and collapse multi-line label text."""
    text = label.replace("*", " ").strip()
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return text
    # Wrapping labels pick up the option text too; the first line is the ask.
    return lines[0][:300]


def read_fields(page: Page, root_selector: str | None = None) -> list[FormField]:
    """Every fillable control on the page (or within `root_selector`)."""
    try:
        raw = page.evaluate(_EXTRACT_JS, root_selector)
    except Exception:
        return []

    fields: list[FormField] = []
    for item in raw or []:
        label = _clean_label(item.get("label", ""))
        kind = item.get("kind", "text")
        if not label and kind != "file":
            continue
        fields.append(
            FormField(
                question=Question(
                    question=label or "Resume",
                    kind=kind,
                    required=bool(item.get("required")),
                    options=list(item.get("options") or []),
                    field_hint=item.get("name", ""),
                ),
                selector=item.get("sel", ""),
                kind=kind,
                options=list(item.get("options") or []),
                name=item.get("name", ""),
            )
        )
    return fields


# --------------------------------------------------------------------------
# Filling
# --------------------------------------------------------------------------


class FillError(RuntimeError):
    pass


def fill_field(page: Page, field: FormField, value: str, *, timeout: int = 10_000) -> None:
    """Type one answer into one control."""
    locator = page.locator(field.selector).first

    if field.kind == "choice" and field.options:
        if field.selector.startswith("input[type=\"radio\"]"):
            _select_radio(page, field, value)
            return
        _select_option(locator, field, value, timeout=timeout)
        return

    if field.kind == "bool":
        wants_yes = str(value).strip().lower() in {"yes", "true", "1", "y", "on"}
        if wants_yes != locator.is_checked(timeout=timeout):
            locator.click(timeout=timeout)
        return

    locator.fill(value, timeout=timeout)


def _select_option(locator, field: FormField, value: str, *, timeout: int) -> None:
    """Pick the closest matching <option>.

    Exact match, then case-insensitive, then substring — forms word options
    inconsistently ("Yes" / "yes" / "Yes, I am authorized").
    """
    wanted = value.strip().lower()
    match = next((o for o in field.options if o.strip().lower() == wanted), None)
    if match is None:
        match = next((o for o in field.options if wanted in o.strip().lower()), None)
    if match is None:
        match = next((o for o in field.options if o.strip().lower() in wanted), None)
    if match is None:
        raise FillError(
            f"No option on {field.question.question!r} matches {value!r}. "
            f"Available: {', '.join(field.options[:8])}"
        )
    locator.select_option(label=match, timeout=timeout)


def _select_radio(page: Page, field: FormField, value: str) -> None:
    group = page.locator(field.selector)
    wanted = value.strip().lower()
    for i in range(group.count()):
        radio = group.nth(i)
        label = (radio.get_attribute("value") or "").strip().lower()
        if label == wanted or wanted in label or (label and label in wanted):
            radio.check()
            return
    raise FillError(f"No radio option on {field.question.question!r} matches {value!r}")


def upload_resume(page: Page, resume_path, selectors: tuple[str, ...] = ()) -> bool:
    """Attach the resume to the first file input that will take it."""
    candidates = selectors or (
        "input[type='file'][name*='resume' i]",
        "input[type='file'][id*='resume' i]",
        "input[type='file'][name*='cv' i]",
        "input[type='file']",
    )
    for selector in candidates:
        locator = page.locator(selector)
        if not locator.count():
            continue
        try:
            locator.first.set_input_files(str(resume_path))
            page.wait_for_timeout(1_500)
            return True
        except Exception:
            continue
    return False
