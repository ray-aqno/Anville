// The traveler's note back to Anville (DESIGN.md §4.8). The page itself is rendered by sheet.py; this file only
// sends the machinist's answer. notePayload is pure, so it is tested with node --test.

const TOOL_ARRIVED = "tool_arrived";

// What one button sends. "As planned" carries only the ticked tools; "different" carries the whole note.
export function notePayload({ sheetId, requestId, outcome, tools, tags, setupsRun, text }) {
  const different = outcome === "different";
  const tagged = new Set(different ? tags : []);
  if (tools.length) tagged.add(TOOL_ARRIVED);
  const runs = Number(setupsRun);
  return {
    request_id: requestId, sheet_id: sheetId, outcome,
    tools_arrived: [...new Set(tools)], tags: [...tagged],  // a cutter in two setups is one purchase
    setups_run: different && Number.isInteger(runs) && runs >= 1 ? runs : null,
    note_text: different ? text : "",
  };
}

// The request id for this note. A retry keeps the id only while the note is unchanged: the server keeps the first
// version it recorded under an id, so an edited retry under the same id would be reported sent and then dropped.
export function requestFor(pending, note, newId) {
  const content = JSON.stringify(note);
  return pending && pending.content === content ? pending : { id: newId(), content };
}

// The flagged face's picture lands after the comment (ADR 0007 Amendment 4). A sheet opened first has a hidden
// slot: ask again every few seconds for a minute, and show it once it loads (Codex, PR #51).
export function watchPicture(figure, { every = 5000, tries = 12, wait = setTimeout } = {}) {
  const img = figure.querySelector("img");
  let n = 0;
  const next = () => { n += 1; img.src = `${figure.dataset.render}&try=${n}`; };
  img.onload = () => { figure.hidden = false; };
  img.onerror = () => { if (n < tries) wait(next, every); };
  next();
}

function wire(main) {
  const form = main.querySelector("form.note");
  const status = form.querySelector(".sent");
  const fieldset = form.querySelector("fieldset.different");
  let pending = null;  // {id, content}: reused on a retry of the same note, so a resend never records it twice

  form.querySelector("[data-open]").addEventListener("click", () => {
    fieldset.hidden = false;
    fieldset.querySelector("textarea").focus();
  });

  form.addEventListener("click", async (ev) => {
    const button = ev.target.closest("[data-outcome]");
    if (!button) return;
    const outcome = button.dataset.outcome;
    const text = form.querySelector("textarea").value;
    if (outcome === "different" && !text.trim()) {
      status.textContent = "Say what was different, in a few words.";
      return;
    }
    const note = {
      outcome, text,
      tools: [...main.querySelectorAll("[data-arrived]:checked")].map((b) => b.dataset.arrived),
      tags: [...form.querySelectorAll('input[name="tag"]:checked')].map((b) => b.value),
      setupsRun: form.querySelector('input[name="setups_run"]').value,
    };
    pending = requestFor(pending, note, () => crypto.randomUUID());
    const body = notePayload({ ...note, sheetId: main.dataset.sheet, requestId: pending.id });
    for (const b of form.querySelectorAll("button")) b.disabled = true;
    status.textContent = "Sending…";
    try {
      const res = await fetch(`/api/shop-note?s=${encodeURIComponent(main.dataset.sig)}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) {
        // The server answered: retrying the same note won't help, so say so instead of blaming the connection.
        status.textContent = res.status === 404 ? "This sheet link isn't valid any more. Ask the engineer for a new one."
          : `Anville couldn't take this note (error ${res.status}). Check what was entered and try again.`;
        for (const b of form.querySelectorAll("button")) b.disabled = false;
        return;
      }
      form.classList.add("done");
      status.textContent = "Thanks. Anville will use this on the next review of this part.";
    } catch {
      for (const b of form.querySelectorAll("button")) b.disabled = false;
      status.textContent = "Didn't send. Check the connection and tap again; it won't be counted twice.";
    }
  });
}

if (typeof document !== "undefined") {
  const main = document.querySelector("main[data-sheet]");
  if (main) wire(main);
  const slot = document.querySelector("figure.face[data-render]");
  if (slot) watchPicture(slot);
}
