import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
  vus: 10,
  duration: '20s',
};

export default function stress_auth_flow() {
  const uniqueId = `${__VU}-${__ITER}-${Date.now()}`;
  const email = `stress-${uniqueId}@example.com`;
  const username = `stress-${uniqueId}`;
  const initialPassword = 'password123';
  const newPassword = 'newpassword123';

  const signupPayload = JSON.stringify({
    email,
    username,
    full_name: 'Stress User',
    password: initialPassword,
  });

  const signupRes = http.post('http://127.0.0.1:8000/auth/signup/init', signupPayload, {
    headers: { 'Content-Type': 'application/json' },
  });

  if (signupRes.status === 200) {
    const verifyPayload = JSON.stringify({
      email,
      otp_code: '123456',
    });

    const verifyRes = http.post('http://127.0.0.1:8000/auth/signup/verify', verifyPayload, {
      headers: { 'Content-Type': 'application/json' },
    });

    if (verifyRes.status === 200) {
      const loginPayload = JSON.stringify({
        identifier: email,
        password: initialPassword,
      });

      const loginRes = http.post('http://127.0.0.1:8000/auth/login', loginPayload, {
        headers: { 'Content-Type': 'application/json' },
      });

      if (loginRes.status === 200) {
        const authHeader = loginRes.headers.Authorization || loginRes.headers.authorization;
        if (authHeader) {
          http.post('http://127.0.0.1:8000/auth/logout', '', {
            headers: {
              'Content-Type': 'application/json',
              Authorization: authHeader,
            },
          });
        }
      }
    }
  }

  const forgotPayload = JSON.stringify({ email });
  const forgotRes = http.post('http://127.0.0.1:8000/auth/password-reset/request', forgotPayload, {
    headers: { 'Content-Type': 'application/json' },
  });

  if (forgotRes.status === 200) {
    const resetBody = forgotRes.json();
    const resetToken = resetBody.reset_token;

    if (resetToken) {
      const resetPayload = JSON.stringify({
        email,
        reset_token: resetToken,
        new_password: newPassword,
      });

      const resetRes = http.post('http://127.0.0.1:8000/auth/password-reset/verify', resetPayload, {
        headers: { 'Content-Type': 'application/json' },
      });

      if (resetRes.status === 200) {
        const loginAfterResetPayload = JSON.stringify({
          identifier: email,
          password: newPassword,
        });

        const loginAfterResetRes = http.post('http://127.0.0.1:8000/auth/login', loginAfterResetPayload, {
          headers: { 'Content-Type': 'application/json' },
        });

        if (loginAfterResetRes.status === 200) {
          const authHeader = loginAfterResetRes.headers.Authorization || loginAfterResetRes.headers.authorization;
          if (authHeader) {
            http.post('http://127.0.0.1:8000/auth/logout', '', {
              headers: {
                'Content-Type': 'application/json',
                Authorization: authHeader,
              },
            });
          }
        }
      }
    }
  }

  sleep(0.1);
}


