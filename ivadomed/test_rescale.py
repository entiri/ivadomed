import pytest
import numpy as np
from .maths import rescale_values_array


# test rescale function
@pytest.mark.parametrize("test_input, expected", [(np.ones((4, 4)), np.zeros((4, 4)))])
def test_eval(test_input, expected):
	assert np.sum(rescale_values_array(test_input)) == np.sum(expected)

