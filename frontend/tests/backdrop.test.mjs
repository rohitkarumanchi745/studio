// A modal closes on a click on its backdrop, never on a press that began
// inside the dialog (selecting text in a field) and was released outside it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { backdropClose } from "../src/backdrop.js";

function backdrop() {
  const el = { dataset: {} };
  const closed = [];
  const props = backdropClose(() => closed.push(true));
  return {
    el, closed,
    press(target) { props.onMouseDown({ target, currentTarget: el }); },
    click(target) { props.onClick({ target, currentTarget: el }); },
  };
}

test("a click that starts and ends on the backdrop closes the modal", () => {
  const b = backdrop();
  b.press(b.el);
  b.click(b.el);
  assert.equal(b.closed.length, 1);
});

test("a drag from a field to the backdrop does not close it", () => {
  const b = backdrop();
  const input = { tagName: "INPUT" };
  b.press(input);          // mousedown inside the dialog
  b.click(b.el);           // the browser reports the click on the common ancestor
  assert.equal(b.closed.length, 0);
  // …and does not arm the next click either.
  b.click(b.el);
  assert.equal(b.closed.length, 0);
});

test("a press on the backdrop released inside the dialog does not close it", () => {
  const b = backdrop();
  b.press(b.el);
  b.click({ tagName: "DIV" });
  assert.equal(b.closed.length, 0);
});

test("the press is kept on the element, so a re-render in between keeps it", () => {
  const b = backdrop();
  b.press(b.el);
  const rerendered = backdropClose(() => b.closed.push(true));   // new handlers, same element
  rerendered.onClick({ target: b.el, currentTarget: b.el });
  assert.equal(b.closed.length, 1);
});
