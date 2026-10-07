import {createContext, useContext} from 'react';
export const Permissions = createContext(true);
export const useCanWrite = () => useContext(Permissions);
