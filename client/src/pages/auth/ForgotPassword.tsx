import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";

import "./ForgotPassword.css";

type ForgotPasswordProps = {
  onBackToSignIn: () => void;
};

const ForgotPassword = ({ onBackToSignIn }: ForgotPasswordProps) => {
  const [email, setEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const handleReset = (e: React.FormEvent) => {
    e.preventDefault();

    if (!email.trim()) {
      setError("Email is required");
      return;
    }

    if (!newPassword) {
      setError("New password is required");
      return;
    }

    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }

    const accountsJson = localStorage.getItem("ingester-accounts");
    const accounts = accountsJson ? JSON.parse(accountsJson) : [];

    const idx = accounts.findIndex((a: any) => a.email === email.trim());

    if (idx === -1) {
      setError("No account found for this email");
      return;
    }

    accounts[idx].password = newPassword;
    localStorage.setItem("ingester-accounts", JSON.stringify(accounts));

    setMessage("Password reset successfully. Redirecting to Sign In...");

    setTimeout(() => {
      onBackToSignIn();
    }, 1200);
  };

  return (
    <div className="forgot-page">
      <main className="forgot-container">
        <h1>Reset Password</h1>

        <form className="forgot-form" onSubmit={handleReset}>
          <label>Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => {
              setEmail(e.target.value);
              setError("");
            }}
          />

          <label>New Password</label>
          <div className="input-with-icon">
            <input
              type={showPassword ? "text" : "password"}
              value={newPassword}
              onChange={(e) => {
                setNewPassword(e.target.value);
                setError("");
              }}
            />

            <button
              type="button"
              className="password-icon"
              onClick={() => setShowPassword((s) => !s)}
              aria-label="Toggle password visibility"
            >
              {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>

          {error && <p className="forgot-error">{error}</p>}
          {message && <p className="forgot-message">{message}</p>}

          <div className="forgot-actions">
            <button type="submit" className="reset-btn">Reset Password</button>
            <button type="button" className="back-btn" onClick={onBackToSignIn}>Back to Sign In</button>
          </div>
        </form>
      </main>
    </div>
  );
};

export default ForgotPassword;
