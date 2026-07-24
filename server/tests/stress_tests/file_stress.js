import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
  vus: 5,
  duration: '10s',
};

export default function () {
  const payload = JSON.stringify({
    name: `dataset-${__VU}-${__ITER}`,
    description: 'Stress dataset',
  });

  http.post('http://127.0.0.1:8000/v1/datasets', payload, {
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer test-token' },
  });
  sleep(0.1);
}
