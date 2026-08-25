import { createClient } from 'redis';
import axios from 'axios';

export async function ping(): Promise<string> {
  const client = createClient();
  await client.connect();
  return await (await axios.get('/ping')).data;
}
