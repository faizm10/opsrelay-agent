export function isPublicPage(path: string) {
  return path === "/" || path === "/sign-in" || path === "/sign-up";
}
