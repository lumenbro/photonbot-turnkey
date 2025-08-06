FROM python:3.9-alpine
RUN apk add --no-cache gcc musl-dev linux-headers
RUN pip3 install --upgrade pip
RUN pip3 install stellar-sdk==12.2.0 boto3 cryptography
COPY mock_enclave_server.py .
CMD ["/usr/local/bin/python3", "mock_enclave_server.py"]
