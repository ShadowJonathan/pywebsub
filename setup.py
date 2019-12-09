import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
      name="pywebsub",
      version="0.0.1",
      author="Jonathan de Jong",
      author_email="jonathan@automatia.nl",
      description="WebSub Client for python, based off of gohubbub",
      long_description=long_description,
      long_description_content_type="text/markdown",
      url="https://github.com/ShadowJonathan/pywebsub",
      packages=setuptools.find_packages(),
      classifiers=[
            "Programming Language :: Python :: 3",
            "License :: OSI Approved :: MIT License",
            "Operating System :: OS Independent",
      ],
      install_requires=[
            'sanic',
            'httpx'
      ],
      setup_requires=['wheel'],
      extras_require={
            'dev': [
                  'ipython'
            ]
      }
)