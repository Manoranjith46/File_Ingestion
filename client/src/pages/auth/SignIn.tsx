import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import "./SignIn.css";

type SignInProps = {
  onSignIn: () => void;
  onCreateAccount: () => void;
  onForgotPassword?: () => void;
};

const SignIn = ({ onSignIn, onCreateAccount, onForgotPassword }: SignInProps) => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const [showPassword, setShowPassword] =
    useState(false);

  const [error, setError] = useState("");

  const handleSubmit = (
    event: React.FormEvent<HTMLFormElement>
  ) => {
    event.preventDefault();

    if (!email.trim()) {
      setError("Email is required");
      return;
    }

    if (!password.trim()) {
      setError("Password is required");
      return;
    }

    // validate against stored accounts (frontend-only)
    const accountsJson = localStorage.getItem(
      "ingester-accounts"
    );

    const accounts = accountsJson
      ? JSON.parse(accountsJson)
      : [];

    const matched = accounts.find(
      (a: any) => a.email === email.trim()
    );

    if (!matched) {
      setError(
        "No account found for this email"
      );
      return;
    }

    if (matched.password !== password) {
      setError("Incorrect password");
      return;
    }

    setError("");
    // store current signed-in user for profile/history
    localStorage.setItem("ingester-current-user", matched.email);

    onSignIn();
  };

  return (
    <div className="signin-page">
      <div className="background-grid"></div>

      <div className="glow glow-one"></div>
      <div className="glow glow-two"></div>

      <div className="signin-card">
        {/* Brand */}
        <div className="signin-brand">
          <div className="signin-logo">I</div>
          <span>Ingester Platform</span>
        </div>

        {/* Header */}
        <div className="signin-header">
          <p className="eyebrow">
            WELCOME BACK
          </p>

          <h1>
            Sign in to <span>Ingester.</span>
          </h1>

          <p>
            Continue your data workflow journey.
          </p>
        </div>

        {/* Form */}
        <form
          className="signin-form"
          onSubmit={handleSubmit}
        >
          {/* Email */}
          <div className="signin-input-group">
            <label>Email</label>

            <div className="signin-input-wrapper">
              <span className="signin-input-icon">
                @
              </span>

              <input
                type="email"
                placeholder="you@company.com"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  setError("");
                }}
              />
            </div>
          </div>

          {/* Password */}
          <div className="signin-input-group">
            <div className="password-label-row">
              <label>Password</label>

              <button
                type="button"
                className="forgot-password"
                onClick={() => onForgotPassword && onForgotPassword()}
              >
                Forgot password?
              </button>
            </div>

            <div className="signin-input-wrapper">
              <span className="signin-input-icon">
                ⌁
              </span>

              <input
                type={
                  showPassword
                    ? "text"
                    : "password"
                }
                placeholder="Enter your password"
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                  setError("");
                }}
              />

              <button
                type="button"
                className="password-icon"
                onClick={() =>
                  setShowPassword(
                    (previousValue) =>
                      !previousValue
                  )
                }
                aria-label={
                  showPassword
                    ? "Hide password"
                    : "Show password"
                }
              >
                {showPassword ? (
                  <EyeOff size={18} />
                ) : (
                  <Eye size={18} />
                )}
              </button>
            </div>
          </div>

          {/* Error */}
          {error && (
            <p className="signin-error">
              {error}
            </p>
          )}

          {/* Button */}
          <button
            type="submit"
            className="signin-btn"
          >
            <span>Sign In</span>
            <span className="arrow">→</span>
          </button>
        </form>

        {/* Create Account */}
        <p className="create-account-text">
  Don't have an account?

  <button
    type="button"
    className="create-account-link"
    onClick={onCreateAccount}
  >
    Create account
  </button>
</p>

        {/* Footer */}
        <p className="signin-footer">
          Secure data ingestion. Built for modern workflows.
        </p>
      </div>
    </div>
  );
};

export default SignIn;