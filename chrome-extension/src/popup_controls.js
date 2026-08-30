export function applyBusyState(
  controls,
  serverRouteButtons,
  { busy, serverRouteAvailable },
) {
  for (const control of controls) {
    if (control) control.disabled = Boolean(busy);
  }
  for (const control of serverRouteButtons) {
    if (control) control.disabled = Boolean(busy) || !serverRouteAvailable;
  }
}
