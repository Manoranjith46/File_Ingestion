import http from 'k6/http';
import { sleep } from 'k6';

export const options = {
  vus: 5,
  duration: '10s',
};

export default function () {
  const datasetName = `stress-ds-${__VU}-${__ITER}`;
  const payload = JSON.stringify({ name: datasetName, language: 'English' });

  const params = {
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer test-token', // placeholder token; replace with real token if needed
    },
  };

  http.post('http://127.0.0.1:8000/v1/datasets', payload, params);
  sleep(0.1);
}
