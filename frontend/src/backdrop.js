// A modal's backdrop closes it only on a click that both STARTS and ENDS on
// the backdrop. A plain onClick also fires when a press begins inside the
// dialog — dragging to select text in a field — and is released over the
// backdrop, which closed half-filled forms (Data connections, API keys). The
// press is remembered on the element, not in state, so a re-render between
// press and release cannot lose it.
export function backdropClose(onClose) {
  return {
    onMouseDown: (e) => {
      e.currentTarget.dataset.pressed = e.target === e.currentTarget ? "1" : "";
    },
    onClick: (e) => {
      const pressed = e.currentTarget.dataset.pressed === "1";
      e.currentTarget.dataset.pressed = "";
      if (pressed && e.target === e.currentTarget) onClose();
    },
  };
}
