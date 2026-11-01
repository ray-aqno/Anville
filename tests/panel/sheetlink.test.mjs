// The card's "Open setup sheet" (ADR 0007 Amendment 5): main puts the signed sheet link on "Setup sheet ready"; the
// panel can't sign one itself, since the secret never reaches the page.
import { test } from "node:test";
import assert from "node:assert/strict";
import { latestRun } from "../../anville/panel/static/reduce.js";
import { renderHTML } from "../../anville/panel/static/view.js";
import { fold, load } from "./fixtures.mjs";

const LIVE = load("part_a_live").events;
const at = LIVE.findIndex((e) => e.title === "Posted the review");  // main stores the sheet just before posting
const SIG = "a".repeat(64);
const URL = `https://anville.example.modal.run/sheet/3f2a9c0d1e2b4a5f6c7d8e9f0a1b2c3d?s=${SIG}`;

function withSheet(detail) {
  const ready = { ...LIVE[at - 1], kind: "tool_result", title: "Setup sheet ready", face_ids: [],
    detail: { sheet_id: "3f2a9c0d1e2b4a5f6c7d8e9f0a1b2c3d", setups: 1, buying: ["910480"], ...detail } };
  // Cursors stay increasing: shift every event from the insertion point on by one.
  const shifted = LIVE.slice(at - 1).map((e) => ({ ...e, cursor: e.cursor + 1, seq: e.seq + 1 }));
  return [...LIVE.slice(0, at - 1), { ...ready, cursor: LIVE[at - 1].cursor, seq: LIVE[at - 1].seq }, ...shifted];
}

test("the run keeps the sheet main stored, with its signed link", () => {
  const run = latestRun(fold(withSheet({ sheet_url: URL })));
  assert.deepEqual(run.sheet, { id: "3f2a9c0d1e2b4a5f6c7d8e9f0a1b2c3d", url: URL, setups: 1 });
});

test("the card's button opens the sheet in a new tab once the link is there", () => {
  const html = renderHTML(fold(withSheet({ sheet_url: URL })));
  assert.match(html, new RegExp(`<a class="sheet" href="${URL.replace(/[.?]/g, "\\$&")}" target="_blank" rel="noopener noreferrer">Open setup sheet</a>`));
  assert.doesNotMatch(html, /<button class="sheet" disabled>/);
});

test("before the sheet is stored, or without a panel token, the button stays disabled", () => {
  assert.match(renderHTML(fold(LIVE)), /<button class="sheet" disabled>Open setup sheet<\/button>/);
  assert.match(renderHTML(fold(withSheet({ sheet_url: null }))), /<button class="sheet" disabled>/);
});

test("a link that isn't a signed sheet link is never put in an href", () => {
  for (const bad of ["javascript:alert(1)", `http://anville.example/sheet/x?s=${SIG}`, "https://evil.example/phish",
    `https://a.example/sheet/x?s=${SIG}" onclick="x`, `https://a.example/sheet/x?s=short`]) {
    const html = renderHTML(fold(withSheet({ sheet_url: bad })));
    assert.doesNotMatch(html, /<a class="sheet"/, bad);
    assert.match(html, /<button class="sheet" disabled>/, bad);
  }
});

test("the production link shape (deploy.sheet_link's default host) is accepted", () => {
  const prod = `https://ray-aqno--anville-web.modal.run/sheet/3f2a9c0d1e2b4a5f6c7d8e9f0a1b2c3d?s=${SIG}`;
  assert.match(renderHTML(fold(withSheet({ sheet_url: prod }))), /<a class="sheet" href="https:\/\/ray-aqno--anville-web/);
});
