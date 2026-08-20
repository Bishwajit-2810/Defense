import React from 'react';
import { render } from '@testing-library/react';
import App from './App';

describe('App', () => {
  it('renders without crashing', () => {
    // Assuming the app has a navbar or a main layout with a known text or role.
    // As a generic test, we just ensure it mounts successfully.
    const { container } = render(<App />);
    expect(container).toBeInTheDocument();
  });
});
