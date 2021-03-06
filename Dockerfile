FROM ubuntu:latest
MAINTAINER Xiaohan Song <chef@dark.kitchen>

RUN apt-get update && apt-get install -y \
    iproute2 python3-pip && \
    rm -rf /var/lib/apt/lists/*
RUN pip3 install --upgrade pip

ADD . /code
WORKDIR /code
RUN python3 setup.py install

VOLUME ["/usr/lib"]
EXPOSE 7788
EXPOSE 7789

ENTRYPOINT ["shoutd"]
