from setuptools import setup, find_packages

setup(
    name="shopify_integration",
    version="1.0.0",
    description="Shopify to ERPNext integration with automatic Sales Orders, Payment Entries, Sales Invoices, and India GST compliance.",
    author="Yash Chaurasia",
    author_email="chaurasiayash351@gmail.com",
    url="https://github.com/greninja87/shopify_integration_erpnext",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    python_requires=">=3.10",
    license="gpl-3.0",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Framework :: Frappe",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Programming Language :: Python :: 3",
    ],
)
