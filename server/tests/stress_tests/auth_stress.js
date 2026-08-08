import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
  vus: 10,
  duration: '10s',
};

export default function () {
  const payload = JSON.stringify({
    email: `stress_user_${__VU}_${__ITER}@example.com`,
    username: `stress_user_${__VU}_${__ITER}`,
    password: 'StressPass123!',
  });

  const params = { headers: { 'Content-Type': 'application/json' } };

  http.post('http://127.0.0.1:8000/auth/signup/init', payload, params);
  sleep(0.1);
}
