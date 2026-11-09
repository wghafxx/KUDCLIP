import axios from "axios";

export const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || "http://localhost:8001";
export const API = `${BACKEND_URL}/api`;

export const api = axios.create({ baseURL: API, timeout: 120000 });

export const FONT_OPTIONS = [
  { value: "montserrat_italic", label: "Montserrat Black Italic" },
];

export const fontFamily = () => "'Kudclip Montserrat', sans-serif";
