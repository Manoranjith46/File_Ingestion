import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
  vus: 50,
  duration: '20s',
};

// export default function () {
//   const payload = JSON.stringify({
//     email: `stress${__VU}${__ITER}@example.com`,
//     username: `stress${__VU}${__ITER}`,
//     full_name: 'Stress User',
//     password: 'password123',
//   });

//   http.post('http://127.0.0.1:8000/auth/signup/init', payload, {
//     headers: { 'Content-Type': 'application/json' },
//   });
//   sleep(0.1);
// }

export default function () {
  const payload = JSON.stringify({
    email: `stress${__VU}${__ITER}@example.com`,
    password: 'password123',
  });

  http.post('http://127.0.0.1:8000/auth/login', payload, {
    headers: { 'Content-Type': 'application/json' },
  });
  sleep(0.1);
}
