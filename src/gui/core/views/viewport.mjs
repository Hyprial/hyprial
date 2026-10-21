// Phone keyboard / dynamic-viewport bridge for the static Core page.
//
// CSS alone cannot keep a fixed-height shell above the on-screen keyboard on
// every WebView: `100dvh` follows browser chrome in modern engines, but iOS
// WKWebView and some Android WebViews report the keyboard only through
// `visualViewport`. This module publishes the visual viewport height as one
// CSS custom property; the stylesheet uses it with `100dvh` as the fallback.
//
// The visible area can also start below the layout viewport: iOS pans the
// visual viewport (without resizing it) to reveal a focused composer above the
// keyboard. Publishing that visible top lets the stylesheet keep the shell
// aligned instead of leaving it scrolled off-screen. pageTop already includes
// document scroll; offsetTop + scrollY is the fallback for older engines.
//
// It is presentation only: no identity, no storage, no network, and a no-op
// in the test DOM (which has neither `defaultView` nor a styled root).

function visibleTop(visual, view) {
  const pageTop = Number.isFinite(visual?.pageTop) ? visual.pageTop : 0;
  const offset = Number.isFinite(visual?.offsetTop) ? visual.offsetTop : 0;
  const scroll = Number.isFinite(view.scrollY) ? view.scrollY : 0;
  // pageTop already includes document scroll; offsetTop + scrollY covers
  // engines that do not expose it. Taking the larger of the two avoids leaving
  // the shell above the visible area if one of them is stale during a keyboard
  // transition.
  return Math.max(0, pageTop, offset + scroll);
}

export function bindViewport({ document } = {}) {
  const root = document?.documentElement;
  const view = document?.defaultView;
  if (!root || !view || typeof root.style?.setProperty !== 'function') return () => {};

  const visual = view.visualViewport;
  const update = () => {
    const height = visual?.height ?? view.innerHeight;
    if (typeof height === 'number' && Number.isFinite(height) && height > 0) {
      root.style.setProperty('--core-viewport-height', `${Math.round(height)}px`);
    }
    root.style.setProperty('--core-viewport-top', `${Math.round(visibleTop(visual, view))}px`);
  };

  const listeners = [
    [visual, 'resize'],
    [visual, 'scroll'],
    [view, 'resize'],
    [view, 'scroll'],
    [view, 'orientationchange'],
  ].filter(([target]) => target && typeof target.addEventListener === 'function');

  for (const [target, type] of listeners) target.addEventListener(type, update);
  update();
  return () => {
    for (const [target, type] of listeners) target.removeEventListener(type, update);
  };
}
