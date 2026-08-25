/** @type {import('next').NextConfig} */
module.exports = {
  reactStrictMode: true,
  ...(process.env.NEXT_OUTPUT_STANDALONE ? { output: "standalone" } : {}),
};
