import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";

import "./CreateAccount.css";

type FormData = {
  username: string;
  email: string;
  password: string;
  confirmPassword: string;
};

type FormErrors = {
  username?: string;
  email?: string;
  password?: string;
  confirmPassword?: string;
  terms?: string;
};

type CreateAccountProps = {
  onAccountCreated: (
    sessionId: string,
    mailId: string
  ) => void;
  onSignIn: () => void;
};

const CreateAccount = ({
  onAccountCreated,
  onSignIn,
}: CreateAccountProps) => {
  const [formData, setFormData] = useState<FormData>({
    username: "",
    email: "",
    password: "",
    confirmPassword: "",
  });

  const [errors, setErrors] = useState<FormErrors>({});

  const [isTermsAccepted, setIsTermsAccepted] =
    useState(false);

  const [isLoading, setIsLoading] = useState(false);

  const [isPasswordVisible, setIsPasswordVisible] =
    useState(false);

  const [
    isConfirmPasswordVisible,
    setIsConfirmPasswordVisible,
  ] = useState(false);

  const handleChange = (
    field: keyof FormData,
    value: string
  ) => {
    setFormData((previousData) => ({
      ...previousData,
      [field]: value,
    }));

    setErrors((previousErrors) => ({
      ...previousErrors,
      [field]: "",
    }));
  };

  const BASE_URL = "https://dk927dff-8000.inc1.devtunnels.ms";

  const validateForm = (): FormErrors => {
    const newErrors: FormErrors = {};

    const username = formData.username.trim();
    const email = formData.email.trim();
    const password = formData.password;
    const confirmPassword = formData.confirmPassword;

    /* Username Validation */

    if (!username) {
      newErrors.username = "Username is required";
    } else if (username.length < 3) {
      newErrors.username =
        "Username must be at least 3 characters";
    } else if (username.length > 30) {
      newErrors.username =
        "Username cannot exceed 30 characters";
    } else if (!/^[a-zA-Z0-9._-]+$/.test(username)) {
      newErrors.username =
        "Username can contain only letters, numbers, ., _ and -";
    }

    /* Email Validation */

    if (!email) {
      newErrors.email = "Work email is required";
    } else if (
      !/^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/.test(
        email
      )
    ) {
      newErrors.email =
        "Enter a valid work email address";
    }

    /* Password Validation */

    if (!password) {
      newErrors.password = "Password is required";
    } else if (password.length < 8) {
      newErrors.password =
        "Password must contain at least 8 characters";
    } else if (!/[A-Z]/.test(password)) {
      newErrors.password =
        "Password must contain at least one uppercase letter";
    } else if (!/[a-z]/.test(password)) {
      newErrors.password =
        "Password must contain at least one lowercase letter";
    } else if (!/[0-9]/.test(password)) {
      newErrors.password =
        "Password must contain at least one number";
    } else if (
      !/[!@#$%^&*(),.?":{}|<>]/.test(password)
    ) {
      newErrors.password =
        "Password must contain at least one special character";
    }

    /* Confirm Password Validation */

    if (!confirmPassword) {
      newErrors.confirmPassword =
        "Please confirm your password";
    } else if (password !== confirmPassword) {
      newErrors.confirmPassword =
        "Passwords do not match";
    }

    /* Terms Validation */

    if (!isTermsAccepted) {
      newErrors.terms =
        "You must accept the Terms of Service and Privacy Policy";
    }

    return newErrors;
  };

  const handleSubmit = async (
    event: React.FormEvent<HTMLFormElement>
  ) => {
    event.preventDefault();

    const validationErrors = validateForm();

    if (Object.keys(validationErrors).length > 0) {
      setErrors(validationErrors);
      return;
    }

    setErrors({});
    setIsLoading(true);

    try {
      const response = await fetch(
        `${BASE_URL}/auth/register`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            username: formData.username.trim(),
            email: formData.email.trim(),
            password: formData.password,
          }),
        }
      );

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(
          errorText || "Account creation failed"
        );
      }

      const data = await response.json();

      localStorage.setItem(
        "ingester-accounts",
        JSON.stringify([
          {
            username: formData.username.trim(),
            email: formData.email.trim(),
            password: formData.password,
            createdAt: new Date().toISOString(),
          },
        ])
      );

      onAccountCreated(
        data?.sessionId || "frontend-session",
        data?.mailId || formData.email.trim()
      );
    } catch (error) {
      setErrors({
        email:
          error instanceof Error
            ? error.message
            : "Account creation failed",
      });
    } finally {
      setIsLoading(false);
    }
  };

  const getPasswordStrength = () => {
    const password = formData.password;

    let strength = 0;

    if (password.length >= 8) strength++;
    if (/[A-Z]/.test(password)) strength++;
    if (/[a-z]/.test(password)) strength++;
    if (/[0-9]/.test(password)) strength++;
    if (/[!@#$%^&*(),.?":{}|<>]/.test(password)) strength++;

    return strength;
  };

  const passwordStrength = getPasswordStrength();

  return (
    <div className="auth-page">

      {/* Background */}

      <div className="background-grid"></div>

      <div className="glow glow-one"></div>
      <div className="glow glow-two"></div>

      <div className="data-flow flow-one">
        <span></span>
        <span></span>
        <span></span>
      </div>

      <div className="data-flow flow-two">
        <span></span>
        <span></span>
        <span></span>
      </div>

      {/* Main Container */}

      <main className="auth-container">

        {/* Brand */}

        <div className="brand">

          <div className="brand-logo">
            I
          </div>

          <span>
            Ingester Platform
          </span>

        </div>

        {/* Header */}

        <div className="auth-header">

          <p className="eyebrow">
            WELCOME TO INGESTER
          </p>

          <h1>
            Create your <span>workspace.</span>
          </h1>

          <p className="subtitle">
            Build a smarter data workflow for your entire team.
          </p>

        </div>

        {/* Card */}

        <div className="auth-card">

          <form
            className="auth-form"
            onSubmit={handleSubmit}
          >

            {/* Username */}

            <div className="input-group">

              <label>
                Username
              </label>

              <div
                className={`input-wrapper ${
                  errors.username
                    ? "input-error"
                    : ""
                }`}
              >

                <span className="input-icon">
                  
                </span>

                <input
                  type="text"
                  placeholder=" ♧ Enter your username"
                  value={formData.username}
                  onChange={(event) =>
                    handleChange(
                      "username",
                      event.target.value
                    )
                  }
                />

              </div>

              {errors.username && (
                <p className="error-message">
                  {errors.username}
                </p>
              )}

            </div>

            {/* Work Email */}

            <div className="input-group">

              <label>
                Work Email
              </label>

              <div
                className={`input-wrapper ${
                  errors.email
                    ? "input-error"
                    : ""
                }`}
              >

                <span className="input-icon">
                  
                </span>

                <input
                  type="email"
                  placeholder="@ you@company.com"
                  value={formData.email}
                  onChange={(event) =>
                    handleChange(
                      "email",
                      event.target.value
                    )
                  }
                />

              </div>

              {errors.email && (
                <p className="error-message">
                  {errors.email}
                </p>
              )}

            </div>

            {/* Password */}

            <div className="input-group">

              <label>
                Password
              </label>

              <div
                className={`input-wrapper ${
                  errors.password
                    ? "input-error"
                    : ""
                }`}
              >

                <span className="input-icon">
                  
                </span>

                <input
                  type={
                    isPasswordVisible
                      ? "text"
                      : "password"
                  }
                  placeholder=" ⌁ Create a strong password"
                  value={formData.password}
                  onChange={(event) =>
                    handleChange(
                      "password",
                      event.target.value
                    )
                  }
                />

                <button
                  type="button"
                  className="password-icon"
                  onClick={() =>
                    setIsPasswordVisible(
                      (previousValue) =>
                        !previousValue
                    )
                  }
                  aria-label={
                    isPasswordVisible
                      ? "Hide password"
                      : "Show password"
                  }
                >
                  {isPasswordVisible ? (
                    <EyeOff size={18} />
                  ) : (
                    <Eye size={18} />
                  )}
                </button>

              </div>

              {/* Password Strength */}

              <div className="password-strength">

                {[1, 2, 3, 4, 5].map((level) => (
                  <span
                    key={level}
                    className={
                      passwordStrength >= level
                        ? "strength-active"
                        : ""
                    }
                  ></span>
                ))}

                <p>
                  {passwordStrength === 0 &&
                    "Use 8 or more characters"}

                  {passwordStrength === 1 &&
                    "Very weak"}

                  {passwordStrength === 2 &&
                    "Weak"}

                  {passwordStrength === 3 &&
                    "Medium"}

                  {passwordStrength === 4 &&
                    "Strong"}

                  {passwordStrength === 5 &&
                    "Very strong"}
                </p>

              </div>

              {errors.password && (
                <p className="error-message">
                  {errors.password}
                </p>
              )}

            </div>

            {/* Confirm Password */}

            <div className="input-group">

              <label>
                Confirm Password
              </label>

              <div
                className={`input-wrapper ${
                  errors.confirmPassword
                    ? "input-error"
                    : ""
                }`}
              >

                <span className="input-icon">
                  
                </span>

                <input
                  type={
                    isConfirmPasswordVisible
                      ? "text"
                      : "password"
                  }
                  placeholder=" ⌁ Confirm your password"
                  value={formData.confirmPassword}
                  onChange={(event) =>
                    handleChange(
                      "confirmPassword",
                      event.target.value
                    )
                  }
                />

                <button
                  type="button"
                  className="password-icon"
                  onClick={() =>
                    setIsConfirmPasswordVisible(
                      (previousValue) =>
                        !previousValue
                    )
                  }
                  aria-label={
                    isConfirmPasswordVisible
                      ? "Hide confirm password"
                      : "Show confirm password"
                  }
                >
                  {isConfirmPasswordVisible ? (
                    <EyeOff size={18} />
                  ) : (
                    <Eye size={18} />
                  )}
                </button>

              </div>

              {errors.confirmPassword && (
                <p className="error-message">
                  {errors.confirmPassword}
                </p>
              )}

            </div>

            {/* Terms */}

            <label className="terms">

              <input
                type="checkbox"
                checked={isTermsAccepted}
                onChange={(event) => {
                  setIsTermsAccepted(
                    event.target.checked
                  );

                  setErrors((previousErrors) => ({
                    ...previousErrors,
                    terms: "",
                  }));
                }}
              />

              <span>
                I agree to the{" "}
                <b>Terms of Service</b> and{" "}
                <b>Privacy Policy</b>
              </span>

            </label>

            {errors.terms && (
              <p className="error-message terms-error">
                {errors.terms}
              </p>
            )}

            {/* Create Account Button */}

            <button
              type="submit"
              className="create-btn"
              disabled={isLoading}
            >

              <span>
                {isLoading
                  ? "Creating Account..."
                  : "Create Account"}
              </span>

              <span className="arrow">
                {isLoading
                  ? "..."
                  : "→"}
              </span>

            </button>

          </form>

        </div>

        {/* Sign In */}

        <p className="signin-text">

          Already have an account?

          <button
            type="button"
            className="signin-link"
            onClick={onSignIn}
          >
            Sign in
          </button>

        </p>

        {/* Footer */}

        <p className="footer-text">
          Secure data ingestion. Built for modern workflows.
        </p>

      </main>

    </div>
  );
};

export default CreateAccount