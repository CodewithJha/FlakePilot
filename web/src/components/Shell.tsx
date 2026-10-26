import type { ReactNode } from "react";
import { Mark } from "./Mark";

export function Shell({
  signIn,
  children,
}: {
  signIn: boolean;
  children: ReactNode;
}) {
  return (
    <>
      <a className="skip" href="#content">
        Skip to content
      </a>
      <header className="mast">
        <a className="word" href="/">
          <Mark />
          <span>FlakePilot</span>
        </a>
        {signIn ? (
          <a className="signin" href="/auth/github/login">
            Sign in with GitHub
          </a>
        ) : (
          <p className="mast-note">Read-only · nothing executed</p>
        )}
      </header>
      <main id="content">{children}</main>
    </>
  );
}
