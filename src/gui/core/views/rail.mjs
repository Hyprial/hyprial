// The v11 navigation rail. Two real pages plus one explicitly unavailable
// entry: an entry the node cannot serve is disabled and says why, never a
// button that pretends to work.
import { button as makeButton, element as makeElement, focusNode } from './dom.mjs';

export const RAIL_PAGES = Object.freeze([
  { id: 'chat', label: '聊天' },
  { id: 'contacts', label: '通讯录' },
]);

export const RAIL_UNAVAILABLE = Object.freeze([
  { id: 'tasks', label: '任务', reason: '任务接口尚未接入，本页不提供该能力' },
]);

export function createRail({ document, active = 'chat', onNavigate = () => {} } = {}) {
  const element = (tagName, className = '', text = '') => makeElement(document, tagName, className, text);
  const button = (className, text) => makeButton(document, className, text);
  const nav = element('nav', 'core-nav');
  nav.setAttribute('aria-label', '主导航');
  const buttons = new Map();
  // Keep the page id next to its node instead of reading it back off the DOM:
  // in a real browser `element.attributes['data-page']` is an Attr node, not
  // the string the test double stores, so the identifier is held here.
  const enabled = [];

  for (const page of RAIL_PAGES) {
    const node = button('core-nav-item', page.label);
    node.setAttribute('data-page', page.id);
    node.addEventListener('click', () => onNavigate(page.id));
    buttons.set(page.id, node);
    enabled.push({ id: page.id, node });
    nav.appendChild(node);
  }

  for (const page of RAIL_UNAVAILABLE) {
    const node = button('core-nav-item core-nav-disabled', page.label);
    node.disabled = true;
    node.setAttribute('aria-disabled', 'true');
    node.setAttribute('data-page', page.id);
    node.setAttribute('title', page.reason);
    nav.appendChild(node);
  }

  // A vertical navigation menu: arrows move the selection and activate the
  // target page immediately, Home/End jump to the ends. The DOM buttons remain
  // ordinary buttons, so Enter/Space and tab order still work natively.
  nav.addEventListener('keydown', event => {
    const index = enabled.findIndex(entry => entry.node === event.target);
    if (index < 0) return;
    let next = null;
    if (event.key === 'ArrowDown' || event.key === 'ArrowRight') next = enabled[(index + 1) % enabled.length];
    else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') next = enabled[(index - 1 + enabled.length) % enabled.length];
    else if (event.key === 'Home') next = enabled[0];
    else if (event.key === 'End') next = enabled[enabled.length - 1];
    if (!next) return;
    event.preventDefault?.();
    // Navigate first, then keep focus on the rail: the page switch itself must
    // not move focus into the page, or the next arrow key would leave the menu.
    onNavigate(next.id);
    focusNode(next.node);
  });

  function setActive(page) {
    for (const [id, node] of buttons) node.setAttribute('aria-current', id === page ? 'page' : 'false');
  }

  setActive(active);
  return { element: nav, buttons, setActive };
}
