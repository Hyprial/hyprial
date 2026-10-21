// Small DOM helpers shared by the static Core views.
//
// Everything is created with createElement/textContent; no view assigns
// innerHTML, so a session title or message body from the node can never become
// markup. `focusNode` is a no-op in the test DOM, which keeps the views honest
// about only using the small element surface they are given.
export function element(document, tagName, className = '', text = '') {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function button(document, className, text) {
  const node = element(document, 'button', className, text);
  node.setAttribute('type', 'button');
  return node;
}

export function focusNode(node) {
  if (node && typeof node.focus === 'function') node.focus();
}

/** Toggle the native hidden attribute (and drop it again in the test DOM). */
export function setHidden(node, hidden) {
  if (hidden) node.setAttribute('hidden', '');
  else if (typeof node.removeAttribute === 'function') node.removeAttribute('hidden');
}
