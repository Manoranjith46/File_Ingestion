import { useState } from "react";
import "./VerifyEmail.css";

type VerifyEmailProps = {
  sessionId: string;
  mailId: string;
  onVerified: () => void;
};

const VerifyEmail = ({
  mailId,
  onVerified,
}: VerifyEmailProps) => {
  const [otp, setOtp] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);

  const handleOtpChange = (
    event: React.ChangeEvent<HTMLInputElement>
  ) => {
    const value = event.target.value
      .replace(/\D/g, "")
      .slice(0, 6);

    setOtp(value);
    setError("");
  };

  const handleVerify = () => {
    if (otp.length !== 6) {
      setError("Please enter a valid 6-digit OTP");
      return;
    }

    setIsLoading(true);

    setTimeout(() => {
      setIsLoading(false);
      onVerified();
    }, 800);
  };

  return (
    <div className="verify-page">
      <div className="verify-background"></div>

      <div className="verify-glow verify-glow-one"></div>
      <div className="verify-glow verify-glow-two"></div>

      <main className="verify-container">
        <div className="verify-brand">
          <div className="verify-logo">I</div>

          <span>Ingester Platform</span>
        </div>

        <div className="verify-card">
          <div className="verify-icon">✉</div>

          <p className="verify-eyebrow">
            EMAIL VERIFICATION
          </p>

          <h1>Verify your email</h1>

          <p className="verify-description">
            We sent a verification code to
          </p>

          <p className="verify-email">
            {mailId || "your email address"}
          </p>

          <div className="otp-group">
            <label>Verification Code</label>

            <input
              type="text"
              inputMode="numeric"
              placeholder="Enter 6-digit code"
              value={otp}
              onChange={handleOtpChange}
            />
          </div>

          {error && (
            <p className="verify-error">
              {error}
            </p>
          )}

          <button
            type="button"
            className="verify-btn"
            onClick={handleVerify}
            disabled={isLoading}
          >
            <span>
              {isLoading
                ? "Verifying..."
                : "Verify Email"}
            </span>

            <span className="verify-arrow">
              →
            </span>
          </button>

          <p className="demo-text">
            Frontend demo verification
          </p>
        </div>

        <p className="verify-footer">
          Secure data ingestion. Built for modern workflows.
        </p>
      </main>
    </div>
  );
};

export default VerifyEmail;