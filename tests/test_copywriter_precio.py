import pytest

from copywriter import normalizar_precio


@pytest.mark.parametrize('raw,expected', [
    ('USD 450.50', 'USD 450,50'), ('USD 450', 'USD 450'),
    ('USD 120000.00', 'USD 120.000'), ('USD 120.000', 'USD 120.000'),
    ('USD 120,000', 'USD 120.000'), ('US$ 1,234.56', 'USD 1.234,56'),
    ('U$S 1.234,56', 'USD 1.234,56'), ('Gs. 1.500.000', 'Gs. 1.500.000'),
    ('PYG 1500000', 'Gs. 1.500.000'), ('USD 1.2.3', ''),
    ('USD 0', ''), ('Consultar', ''),
])
def test_price_keeps_source_value_without_multiplying_decimals(raw, expected):
    assert normalizar_precio(raw) == expected
