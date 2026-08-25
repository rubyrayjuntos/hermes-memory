const express = require('express');
export const app = express();
app.get('/', (_req, res) => res.send('ok'));
