/**
 * The single htm <-> Preact binding point. Every component imports `html`
 * and the hooks from here, so the tagged-template parser is bound to exactly
 * one `h` and there is one place that knows where the vendored runtime lives.
 */

import htm from "../../vendor/htm/htm.module.js";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "../../vendor/preact/hooks.module.js";
import {h, render} from "../../vendor/preact/preact.module.js";

export const html = htm.bind(h);
export {render, useCallback, useEffect, useMemo, useRef, useState};
