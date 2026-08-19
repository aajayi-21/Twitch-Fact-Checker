// The single htm↔preact binding point. Every component imports `html` from
// here so the tagged-template parser is bound to exactly one `h`.
import { h } from "preact";
import htm from "htm";

export const html = htm.bind(h);
