/** Full page load (resets all client state), for sign-out and organization switches. */
export function hardNavigate(path: string): void {
  window.location.assign(new URL(path, window.location.origin).toString());
}
