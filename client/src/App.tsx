import { useState } from "react";

import CreateAccount from "./pages/auth/CreateAccount";
import VerifyEmail from "./pages/auth/VerifyEmail";
import SignIn from "./pages/auth/SignIn";
import ForgotPassword from "./pages/auth/ForgotPassword";
import Home from "./pages/Home";

type AuthPage =
  | "create-account"
  | "verify-email"
  | "sign-in"
  | "forgot-password"
  | "home";

function App() {
  const [currentPage, setCurrentPage] =
    useState<AuthPage>("create-account");

  const [signupSessionId, setSignupSessionId] =
    useState<string>("");

  const [signupMailId, setSignupMailId] =
    useState<string>("");

  const handleAccountCreated = (
    sessionId: string,
    mailId: string
  ) => {
    setSignupSessionId(sessionId);
    setSignupMailId(mailId);

    setCurrentPage("verify-email");
  };

  return (
    <>
      {/* CREATE ACCOUNT */}
      {currentPage === "create-account" && (
        <CreateAccount
          onAccountCreated={handleAccountCreated}
          onSignIn={() =>
            setCurrentPage("sign-in")
          }
        />
      )}

      {/* VERIFY EMAIL */}
      {currentPage === "verify-email" && (
        <VerifyEmail
          sessionId={signupSessionId}
          mailId={signupMailId}
          onVerified={() =>
            setCurrentPage("sign-in")
          }
        />
      )}

      {/* SIGN IN */}
      {currentPage === "sign-in" && (
        <SignIn
          onSignIn={() =>
            setCurrentPage("home")
          }
          onCreateAccount={() =>
            setCurrentPage("create-account")
          }
          onForgotPassword={() => setCurrentPage("forgot-password")}
        />
      )}

      {/* FORGOT PASSWORD */}
      {currentPage === "forgot-password" && (
        <ForgotPassword onBackToSignIn={() => setCurrentPage("sign-in")} />
      )}

      {/* HOME */}
      {currentPage === "home" && (
        <Home
          onLogout={() => {
            localStorage.removeItem(
              "ingester-current-user"
            );
            setCurrentPage("sign-in");
          }}
        />
      )}
    </>
  );
}

export default App;