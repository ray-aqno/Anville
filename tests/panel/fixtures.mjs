// Shared by the node tests: load a synthetic trace fixture and fold events through the reducer.
import { readFileSync } from "node:fs";
import { initialState, reduce } from "../../anville/panel/static/reduce.js";

export function load(name) {
  return JSON.parse(readFileSync(new URL(`./fixtures/${name}.synthetic.json`, import.meta.url)));
}

export function fold(events, state = initialState()) {
  return events.reduce(reduce, state);
}

export function runOf(state, runId) {
  return state.runs[runId];
}

// The events up to and including the first one with this title: a run cut off mid-way.
export function upTo(events, title) {
  return events.slice(0, events.findIndex((e) => e.title === title) + 1);
}
